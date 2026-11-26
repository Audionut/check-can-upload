import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import requests

import check_can_upload as app


TORRENT_BODY = b"d8:announce26:https://tracker.test/test/4:infod4:name4:Testee"


def torrent_edition(**overrides):
    return {
        "remasterYear": 1992,
        "remasterRecordLabel": "Epic Associated",
        "remasterCatalogueNumber": "ZK 52959",
        "remasterTitle": "",
        "media": "CD",
        "format": "FLAC",
        "encoding": "Lossless",
        **overrides,
    }


class Clock:
    def __init__(self):
        self.seconds = 100.0

    def monotonic(self):
        return self.seconds

    def sleep(self, seconds):
        self.seconds += seconds


class ScanTest(unittest.TestCase):
    def test_local_ops_file_matches_skip_both_tracker_apis(self):
        red_hash, ops_hash, second_ops_hash, other_hash, wrong_hash = (
            digit * 40 for digit in "12345"
        )
        name = "An Artist - An Album [FLAC]"
        torrents = [
            {
                "hash": torrent_hash,
                "name": torrent_name,
                "content_path": f"D:/Music/{torrent_hash}/Album",
                "total_size": size,
                "tracker": f"https://{host}/announce",
            }
            for torrent_hash, torrent_name, size, host in (
                (red_hash, name, 650, "flacsfor.me"),
                (ops_hash, "Renamed Album", 650, "home.opsfet.ch"),
                (second_ops_hash, name, 700, "other.test"),
                (other_hash, name, 650, "other.test"),
                (wrong_hash, name, 650, "home.opsfet.ch"),
            )
        ]
        red_url = "https://redacted.sh/torrents.php?id=1&torrentid=2#torrent2"
        ops_url = "https://orpheus.network/torrents.php?id=3&torrentid=4#torrent4"

        def webui(endpoint, expected, **params):
            if endpoint == "info":
                return torrents
            torrent_hash = params["hash"]
            if endpoint == "properties":
                return {
                    "comment": {red_hash: red_url, ops_hash: ops_url}.get(torrent_hash)
                }
            if endpoint == "trackers":
                host = (
                    "home.opsfet.ch"
                    if torrent_hash == second_ops_hash
                    else "other.test"
                )
                return [{"url": f"https://{host}/announce"}]
            self.assertEqual("files", endpoint)
            self.assertNotEqual(other_hash, torrent_hash)
            folder = "Renamed Album" if torrent_hash == ops_hash else "Album"
            first, second = (250, 350) if torrent_hash == wrong_hash else (200, 400)
            return [
                {"name": f"{folder}/01.flac", "size": first},
                {"name": f"{folder}/02.flac", "size": second},
                {
                    "name": f"{folder}/cover.jpg",
                    "size": 100 if torrent_hash == second_ops_hash else 50,
                },
            ]

        qbit = Mock()
        qbit.get.side_effect = webui
        red, ops = Mock(), Mock(url=app.OPS, authorization="token")
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(StringIO()):
            output = Path(directory)
            downloads = output / "downloads"
            self.assertEqual(
                0,
                app.scan(
                    qbit, red, ops, output, 1, False, download_ops_torrents=downloads
                ),
            )
            self.assertEqual([], list(downloads.iterdir()))
            (match,) = json.loads(
                next(output.glob(f"*/{app.EXACT_REPORT}.json")).read_text()
            )
            self.assertEqual(red_url, match["red_url"])
            self.assertEqual(torrents[0]["content_path"], match["data_location"])
            self.assertEqual("qbit_client", match["match_basis"])
            self.assertEqual([ops_url], match["ops_urls"])
            self.assertEqual(
                {ops_hash, second_ops_hash},
                {torrent["torrent_hash"] for torrent in match["ops_torrents"]},
            )
            self.assertNotIn("missing_ops_formats", match)
        red.get.assert_not_called()
        ops.get.assert_not_called()

    def test_cli_limits_and_resumes_reports_without_rechecking_completed_torrents(self):
        clock = Clock()
        calls = []
        login_status = 204
        artists = [{"name": "The Artist"}]
        title = 'A "Test" Album — Box'
        path = r"M:\Music\The Artist\Actual folder"
        hashes = [digit * 40 for digit in "12345"]
        names = [
            "Exact copy",
            "Unrelated torrent",
            "Another pressing",
            "Unchecked copy",
            "Unscanned copy",
        ]
        listings = [
            {
                "name": name,
                "hash": torrent_hash,
                "content_path": path,
                "tracker": "https://other.test/announce",
            }
            for name, torrent_hash in zip(names, hashes)
        ]
        comments = [
            "RED: <https://redacted.ch/torrents.php?id=5&amp;torrentid=101#torrent101>",
            "https://redacted.sh.example.com/torrents.php?torrentid=999",
            "https://www.redacted.sh/torrents.php?id=5#torrent102",
            "",
            "https://redacted.sh/torrents.php?torrentid=104",
        ]

        class Server(BaseHTTPRequestHandler):
            def reply(self, status, body, **headers):
                self.send_response(status)
                if not isinstance(body, bytes):
                    self.send_header("Content-Type", "application/json")
                    body = json.dumps(body).encode()
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def success(self, **data):
                self.reply(200, {"status": "success", "response": data})

            def log_message(self, *args):
                pass

            def do_POST(self):
                calls.append((self.path, {}, clock.monotonic()))
                data = self.rfile.read(int(self.headers["Content-Length"]))
                if (
                    self.path != "/qbit/api/v2/auth/login"
                    or data != b"username=user&password=password"
                ):
                    self.reply(200, b"Fails.")
                    return
                self.reply(
                    login_status,
                    b"Ok." if login_status == 200 else b"",
                    **{"Set-Cookie": "QBT_SID_8080=test; path=/qbit"},
                )

            def do_GET(self):
                url = urlsplit(self.path)
                query = {key: values[0] for key, values in parse_qs(url.query).items()}
                calls.append((url.path, query, clock.monotonic()))
                if url.path.startswith("/qbit/"):
                    authenticated = (
                        self.headers.get("Cookie") == "QBT_SID_8080=test"
                        or self.headers.get("Authorization") == "Bearer qbit-key"
                    )
                    if not authenticated or self.headers.get("Origin") != base:
                        self.reply(403, b"Forbidden")
                    elif url.path.endswith("/info"):
                        self.reply(200, listings)
                    elif url.path.endswith("/properties"):
                        self.reply(
                            200, {"comment": comments[hashes.index(query["hash"])]}
                        )
                    else:
                        host = (
                            "flacsfor.me"
                            if query["hash"] == hashes[3]
                            else "other.test"
                        )
                        self.reply(200, [{"url": f"https://{host}/private-announce"}])
                elif (
                    url.path == "/red/ajax.php"
                    and self.headers.get("Authorization") == "red-key"
                ):
                    torrent_id = int(query["id"]) if "id" in query else 103
                    self.success(
                        group={
                            "id": 5,
                            "name": "Later album" if torrent_id == 103 else title,
                            "year": 1992,
                            "recordLabel": "Virgin EMI",
                            "catalogueNumber": "ZK 52959",
                            "musicInfo": {
                                "artists": [{"name": "Later Artist"}]
                                if torrent_id == 103
                                else artists
                            },
                        },
                        torrent={
                            "id": torrent_id,
                            "format": "FLAC",
                            "encoding": "24bit Lossless"
                            if torrent_id == 104
                            else "Lossless",
                            "media": "CD",
                            "size": 5000,
                            **(
                                torrent_edition(
                                    remasterYear=1994,
                                    remasterRecordLabel="Virgin EMI",
                                    remasterCatalogueNumber="ZK 79999",
                                )
                                if torrent_id == 102
                                else {}
                            ),
                        },
                    )
                elif (
                    url.path == "/ops/ajax.php"
                    and self.headers.get("Authorization") == "token ops-key"
                ):
                    if query == {"action": "download", "id": "303"}:
                        self.reply(
                            200,
                            TORRENT_BODY,
                            **{"Content-Type": "application/x-bittorrent"},
                        )
                        return
                    if query == {
                        "action": "artist",
                        "artistname": "Later Artist",
                        "artistreleases": "1",
                    }:
                        self.reply(429, b"Too many requests", **{"Retry-After": "120"})
                        return
                    if query != {
                        "action": "artist",
                        "artistname": "The Artist",
                        "artistreleases": "1",
                    }:
                        self.reply(400, b"Unexpected OPS request")
                        return
                    self.success(
                        torrentgroup=[
                            {
                                "groupName": "A Test Album-Box",
                                "groupId": 2,
                                "groupYear": 1992,
                                "artists": artists,
                                "torrent": [
                                    torrent_edition(
                                        id=301,
                                        format="FLAC",
                                        encoding="Lossless",
                                        size=5000,
                                        remasterYear=1993,
                                        remasterRecordLabel="EMI",
                                        remasterCatalogueNumber="ZK 77777",
                                    ),
                                    torrent_edition(
                                        id=302,
                                        format="MP3",
                                        encoding="320",
                                        remasterRecordLabel="EMI",
                                        remasterCatalogueNumber="ZK-52959",
                                    ),
                                    torrent_edition(
                                        id=304,
                                        remasterRecordLabel="EMI",
                                        remasterCatalogueNumber="ZK-52959",
                                    ),
                                ],
                            },
                            {
                                "groupName": title,
                                "groupId": 3,
                                "groupYear": 1992,
                                "artists": artists,
                                "torrent": [
                                    torrent_edition(
                                        id=303,
                                        format="FLAC",
                                        encoding="Lossless",
                                        remasterRecordLabel="Virgin EMI",
                                        remasterCatalogueNumber="ZK-52959",
                                    ),
                                    torrent_edition(
                                        id=305,
                                        encoding="24bit Lossless",
                                        remasterRecordLabel="Virgin EMI",
                                        remasterCatalogueNumber="ZK-52959",
                                    ),
                                ],
                            },
                        ],
                    )
                else:
                    self.reply(404, b"Not found")

        with (
            ThreadingHTTPServer(("127.0.0.1", 0), Server) as server,
            tempfile.TemporaryDirectory() as tmp,
        ):
            base = f"http://127.0.0.1:{server.server_port}"
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                env = {
                    "QBIT_URL": f"{base}/qbit",
                    "RED_API_KEY": "red-key",
                    "OPS_API_KEY": "ops-key",
                    "QBIT_USERNAME": "user",
                    "QBIT_PASSWORD": "password",
                    "QBIT_API_KEY": "",
                }

                def run_cli(environment, *args):
                    stdout, stderr = StringIO(), StringIO()
                    with (
                        patch.dict(os.environ, environment),
                        patch.object(app, "RED", f"{base}/red"),
                        patch.object(app, "OPS", f"{base}/ops"),
                        patch.object(app, "time", clock),
                        redirect_stdout(stdout),
                        redirect_stderr(stderr),
                    ):
                        status = app.main(["--output-dir", tmp, *args])
                    return status, stdout.getvalue(), stderr.getvalue()

                status, stdout, stderr = run_cli(
                    dict.fromkeys(env, "bad-default"),
                    "--qbit-url",
                    f"{base}/qbit",
                    "--qbit-username",
                    "user",
                    "--qbit-password",
                    "password",
                    "--red-api-key",
                    "red-key",
                    "--ops-api-key",
                    "ops-key",
                    "--limit",
                    "2",
                    "--download-ops-torrents",
                    str(Path(tmp) / "downloads"),
                    "--debug",
                )
                self.assertEqual(0, status, stderr)
                self.assertIn("[RED torrent HTTP 200]", stderr)
                self.assertIn('"recordLabel": "Virgin EMI"', stderr)
                self.assertNotIn("[OPS artist HTTP 200]", stderr)
                self.assertNotIn('"groupName": "A Test Album-Box"', stderr)
                self.assertIn("Reached the limit of 2 new RED torrents", stdout)
                self.assertIn(
                    "[qBittorrent 1/5] Checking RED release 1/2 | Exact copy", stdout
                )
                self.assertIn(
                    f"Searching OPS for The Artist - {title} (1992 / Virgin EMI / ZK 52959 / CD)",
                    stdout,
                )
                self.assertIn(
                    "[qBittorrent 3/5] Checking RED release 2/2 | Another pressing",
                    stdout,
                )
                self.assertNotIn("Unrelated torrent", stdout)
                (report,) = (
                    directory
                    for directory in Path(tmp).iterdir()
                    if app.REPORT_DIRECTORY.fullmatch(directory.name)
                )
                self.assertEqual(
                    {
                        "ops-exact-matches.json",
                        "ops-partial-matches.json",
                        "ops-not-found.json",
                        "ops-unchecked.json",
                    },
                    {file.name for file in report.iterdir()},
                )
                exact_report = report / "ops-exact-matches.json"
                partial_report = report / "ops-partial-matches.json"
                (exact,) = json.loads(exact_report.read_text(encoding="utf-8"))
                self.assertEqual(
                    [], json.loads(partial_report.read_text(encoding="utf-8"))
                )
                (missing,) = json.loads(
                    (report / "ops-not-found.json").read_text(encoding="utf-8")
                )
                self.assertEqual(
                    (f"The Artist - {title}", path, "CD"),
                    (
                        exact["release_name"],
                        exact["data_location"],
                        exact["media"],
                    ),
                )
                self.assertEqual(
                    f"{base}/red/torrents.php?id=5&torrentid=101#torrent101",
                    exact["red_url"],
                )
                self.assertEqual(
                    [f"{base}/ops/torrents.php?id=3&torrentid=303#torrent303"],
                    exact["ops_urls"],
                )
                downloaded = Path(tmp) / "downloads" / "Exact copy.torrent"
                self.assertEqual(TORRENT_BODY, downloaded.read_bytes())
                self.assertEqual(
                    {exact["ops_urls"][0]: str(downloaded.resolve())},
                    exact["ops_torrent_files"],
                )
                self.assertEqual([downloaded], list(downloaded.parent.iterdir()))
                self.assertIs(exact["same_format_and_encoding"], True)
                self.assertIn("OPS exact matches 1, partial matches 0", stdout)
                self.assertEqual(
                    [
                        {"format": "MP3", "encoding": "320"},
                        {"format": "MP3", "encoding": "V0 (VBR)"},
                    ],
                    exact["missing_ops_formats"],
                )
                self.assertEqual(hashes[0], exact["torrent_hash"])
                self.assertEqual("Virgin EMI", exact["record_label"])
                self.assertEqual(
                    ("Another pressing", path, "ZK 79999", []),
                    (
                        missing["torrent_name"],
                        missing["data_location"],
                        missing["catalogue_number"],
                        missing["ops_urls"],
                    ),
                )
                first_red, second_red = [
                    when for url, _, when in calls if url == "/red/ajax.php"
                ]
                self.assertGreaterEqual(second_red - first_red, 4)
                ops_calls = [
                    (params, when)
                    for url, params, when in calls
                    if url == "/ops/ajax.php"
                ]
                self.assertEqual(
                    ["artist", "download"],
                    [params["action"] for params, _ in ops_calls],
                )
                self.assertGreaterEqual(ops_calls[1][1] - ops_calls[0][1], 4)
                login_status = 200
                calls.clear()
                clock.seconds += 61
                status, stdout, stderr = run_cli(env, "--limit", "1", "--debug")
                self.assertEqual(1, status)
                self.assertIn("Unchecked copy", stderr)
                self.assertIn("OPS rate limited the scan", stderr)
                self.assertNotIn("[OPS artist HTTP 429]", stderr)
                self.assertNotIn("Too many requests", stderr)
                self.assertIn("1 qBittorrent torrents left unscanned", stderr)
                self.assertIn("already processed 2", stdout)
                self.assertIn("Checking RED release 1/1 | Unchecked copy", stdout)
                self.assertEqual(
                    [{"action": "torrent", "hash": hashes[3]}],
                    [params for url, params, _ in calls if url == "/red/ajax.php"],
                )
                (unchecked,) = json.loads(
                    (report / "ops-unchecked.json").read_text(encoding="utf-8")
                )
                self.assertEqual(hashes[3], unchecked["torrent_hash"])
                self.assertEqual(path, unchecked["data_location"])
                self.assertEqual(
                    "Later Artist - Later album", unchecked["release_name"]
                )
                self.assertIn("rate limited", unchecked["reason"])

                (report / "ops-matches.csv").write_text(
                    f"torrent_hash\n{hashes[0]}\n", encoding="utf-8-sig"
                )
                exact_report.unlink()
                partial_report.unlink()
                calls.clear()
                clock.seconds += 61
                login_status = 204
                status, stdout, stderr = run_cli(env, "--limit", "1")
                self.assertEqual(0, status, stderr)
                self.assertEqual("", stderr)
                self.assertIn("already processed 3", stdout)
                self.assertIn("Checking RED release 1/1 | Unscanned copy", stdout)
                self.assertNotIn("/ops/ajax.php", [url for url, _, _ in calls])
                (cached_match,) = json.loads(exact_report.read_text(encoding="utf-8"))
                self.assertEqual(
                    [f"{base}/ops/torrents.php?id=3&torrentid=305#torrent305"],
                    cached_match["ops_urls"],
                )
                self.assertEqual(
                    exact["missing_ops_formats"], cached_match["missing_ops_formats"]
                )
                self.assertEqual(
                    [{"action": "torrent", "id": "104"}],
                    [params for url, params, _ in calls if url == "/red/ajax.php"],
                )
                self.assertNotIn(
                    hashes[3],
                    [
                        params.get("hash")
                        for url, params, _ in calls
                        if url.endswith("/properties")
                    ],
                )
                calls.clear()
                clock.seconds += 61
                status, _, stderr = run_cli(
                    env,
                    "--qbit-api-key",
                    "qbit-key",
                    "--rescan",
                    "--new-reports",
                    "--limit",
                    "1",
                )
                self.assertEqual(0, status, stderr)
                self.assertEqual(
                    2,
                    sum(
                        bool(app.REPORT_DIRECTORY.fullmatch(directory.name))
                        for directory in Path(tmp).iterdir()
                    ),
                )
                self.assertIn("/ops/ajax.php", [url for url, _, _ in calls])
                self.assertNotIn(
                    "/qbit/api/v2/auth/login", [url for url, _, _ in calls]
                )
                self.assertEqual(
                    [{"action": "torrent", "id": "101"}],
                    [params for url, params, _ in calls if url == "/red/ajax.php"],
                )
                status, _, stderr = run_cli(env, "--qbit-password", "wrong")
                self.assertEqual(1, status)
                self.assertIn("qBittorrent login rejected (HTTP 200)", stderr)
            finally:
                server.shutdown()
                thread.join()

    def test_torrent_download_preserves_existing_file_and_stops_when_rate_limited(self):
        successful = requests.Response()
        successful.status_code = 200
        successful.headers["Content-Type"] = "text/plain"
        successful._content = TORRENT_BODY
        limited = requests.Response()
        limited.status_code = 200
        limited.headers["Content-Type"] = "application/json"
        limited._content = (
            b'{"status":"failure","error":"Rate limiting hit on downloading"}'
        )
        first, second, third = (app.site_link(app.OPS, 1, i) for i in (10, 11, 12))
        row = {"torrent_name": "Artist: Album", "ops_urls": [first, second, third]}
        ops = app.Gazelle(Mock(), "OPS", app.OPS, "token key", 4)
        with (
            tempfile.TemporaryDirectory() as folder,
            redirect_stdout(StringIO()),
            redirect_stderr(StringIO()),
            patch.object(app, "time", Clock()),
            patch.object(app, "request", side_effect=[successful, limited]) as send,
        ):
            directory = Path(folder).resolve()
            existing = directory / "Artist_ Album.torrent"
            existing.write_bytes(b"previous file")
            downloader = app.OpsTorrentDownloader(ops, directory)
            downloader.save(row)
            self.assertEqual(b"previous file", existing.read_bytes())
            downloaded = directory / "Artist_ Album [OPS 10].torrent"
            self.assertEqual(TORRENT_BODY, downloaded.read_bytes())
            self.assertEqual({first: str(downloaded)}, row["ops_torrent_files"])
            self.assertEqual({second: ops.blocked}, row["ops_download_errors"])
            self.assertEqual({existing, downloaded}, set(directory.iterdir()))
            self.assertEqual(
                [{"action": "download", "id": i} for i in (10, 11)],
                [call.kwargs["params"] for call in send.call_args_list],
            )

    def test_group_lookup_uses_long_window_and_honors_retry_after(self):
        response = Mock(status_code=200, headers={})
        response.json.return_value = {"status": "success", "response": {}}
        throttled = Mock(status_code=429, headers={"Retry-After": "27"})
        throttled.text = "Too many requests"
        clock = Clock()
        times = []

        def send(*args, **kwargs):
            times.append(clock.monotonic())
            return throttled if len(times) == 15 else response

        with (
            requests.Session() as session,
            patch.object(app, "request", side_effect=send),
            patch.object(app, "time", clock),
            redirect_stderr(StringIO()),
        ):
            ops = app.Gazelle(session, "OPS", app.OPS, "key", 4)
            ops.get("torrentgroup", id=1)
            for index in range(14):
                ops.get("artist", artistname=f"Artist {index}")
            ops.get("browse", groupname="Compilation")
        self.assertEqual([*range(100, 153, 4), 161, 188, 205], times)

    def test_omitted_artist_metadata_is_resolved_before_classifying(self):
        artists = [{"name": "The Artist"}]
        wanted = app.Edition.from_api({}, torrent_edition())
        group = {"year": 1992, "musicInfo": {"artists": artists}}
        partial = torrent_edition(id=42)
        del partial["remasterRecordLabel"]
        groups = [
            {
                "groupName": "Album",
                "groupId": 8,
                "groupYear": 1992,
                "artists": artists,
                "torrent": [partial],
            }
        ]
        details = Mock(
            return_value={"group": group, "torrents": [torrent_edition(id=42)]}
        )
        self.assertIn(
            f"{app.OPS}/torrents.php?id=8&torrentid=42#torrent42",
            app.ops_matches(
                groups, details, "Album", ("The Artist",), wanted, ("FLAC", "Lossless")
            ),
        )
        details.assert_called_once_with(8)
        details.return_value["torrents"][0]["remasterRecordLabel"] = ""
        self.assertIn(
            f"{app.OPS}/torrents.php?id=8&torrentid=42#torrent42",
            app.ops_matches(
                groups, details, "Album", ("The Artist",), wanted, ("FLAC", "Lossless")
            ),
        )
        details.return_value["torrents"][0]["media"] = ""
        with self.assertRaisesRegex(
            app.ScanError, r"torrentid=42.*OPS is missing media"
        ):
            app.ops_matches(
                groups, details, "Album", ("The Artist",), wanted, ("FLAC", "Lossless")
            )

    def test_scan_matches_when_both_sites_omit_optional_edition_details(self):
        qbit, red, ops = Mock(), Mock(), Mock()
        artists = [{"name": "The Artist"}]
        qbit.get.side_effect = [
            [
                {
                    "hash": "a" * 40,
                    "name": "The Artist - Album",
                    "content_path": "M:/Music/Album",
                }
            ],
            {"comment": "https://redacted.sh/torrents.php?torrentid=42"},
        ]
        red.get.return_value = {
            "group": {
                "id": 5,
                "name": "Album",
                "year": 2017,
                "musicInfo": {"artists": artists},
            },
            "torrent": {
                "id": 42,
                "remastered": True,
                "remasterYear": 2017,
                "media": "WEB",
                "format": "FLAC",
                "encoding": "Lossless",
            },
        }
        ops.get.return_value = {
            "torrentgroup": [
                {
                    "groupId": 8,
                    "groupName": "Album",
                    "groupYear": 2017,
                    "artists": artists,
                    "torrent": [
                        {
                            "id": 90,
                            "remasterYear": 0,
                            "media": "WEB",
                            "format": "FLAC",
                            "encoding": "Lossless",
                        }
                    ],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(StringIO()):
            output_dir = Path(tmp)
            self.assertEqual(
                0, app.scan(qbit, red, ops, output_dir, limit=1, rescan=False)
            )
            (report,) = output_dir.glob("*/ops-partial-matches.json")
            (match,) = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(
                [f"{app.OPS}/torrents.php?id=8&torrentid=90#torrent90"],
                match["ops_urls"],
            )
            self.assertIn(
                "RED and OPS are missing record label",
                match["match_exceptions"][match["ops_urls"][0]],
            )
        ops.get.assert_called_once_with(
            "artist", artistname="The Artist", artistreleases=1
        )

    def test_various_artists_reports_use_title_search_and_require_matching_edition(
        self,
    ):
        qbit, red, ops = Mock(), Mock(), Mock()
        title = "Compilation Vol. 8"
        names = ("Artist One", "Artist Two", "Artist Three")
        qbit.get.side_effect = [
            [
                {
                    "hash": "a" * 40,
                    "name": f"VA - {title}",
                    "content_path": "M:/Music/Compilation",
                }
            ],
            {"comment": "https://redacted.sh/torrents.php?torrentid=42"},
        ]
        red.get.return_value = {
            "group": {
                "id": 5,
                "name": title,
                "musicInfo": {"artists": [{"name": name} for name in names]},
            },
            "torrent": torrent_edition(id=42),
        }
        uncatalogued = torrent_edition(torrentId=91)
        del uncatalogued["remasterCatalogueNumber"]
        ops.get.side_effect = [
            {
                "currentPage": 1,
                "pages": 2,
                "results": [
                    {
                        "groupId": 7,
                        "groupName": title,
                        "torrents": [
                            torrent_edition(
                                torrentId=70, artists=[{"name": "Solo Artist"}]
                            ),
                        ],
                    },
                    {
                        "groupId": 8,
                        "groupName": title,
                        "artist": "Various Artists",
                        "torrents": [
                            torrent_edition(
                                torrentId=80,
                                remasterYear=1993,
                                remasterCatalogueNumber="ZK 99999",
                            ),
                        ],
                    },
                ],
            },
            {
                "currentPage": 2,
                "pages": 2,
                "results": [
                    {
                        "groupId": 9,
                        "groupName": "Compilation, Vol. 8",
                        "artist": "Various Artists",
                        "torrents": [
                            torrent_edition(
                                torrentId=90, remasterCatalogueNumber="7914512"
                            ),
                            uncatalogued,
                        ],
                    },
                ],
            },
        ]
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(StringIO()):
            output_dir = Path(tmp)
            self.assertEqual(
                0, app.scan(qbit, red, ops, output_dir, limit=1, rescan=False)
            )
            (report,) = output_dir.glob("*/ops-partial-matches.json")
            (match,) = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(f"Various Artists - {title}", match["release_name"])
            self.assertEqual(
                [
                    f"{app.OPS}/torrents.php?id=9&torrentid={torrent_id}#torrent{torrent_id}"
                    for torrent_id in (90, 91)
                ],
                match["ops_urls"],
            )
            first, second = match["ops_urls"]
            self.assertIn("catalogue number differs", match["match_exceptions"][first])
            self.assertIn(
                "OPS is missing catalogue number", match["match_exceptions"][second]
            )
        self.assertEqual(
            [
                ("browse", {"groupname": title, "filter_cat": "Music", "page": page})
                for page in (1, 2)
            ],
            [(call.args[0], call.kwargs) for call in ops.get.call_args_list],
        )

    def test_json_report_preserves_saved_rows_when_append_is_interrupted(self):
        file = BytesIO()
        report = app.JsonReport(file)
        self.assertEqual([], json.loads(file.getvalue()))
        report.append({"release": "First"})
        report.append({"release": "Second"})
        actual_write = file.write

        def interrupted_write(data):
            if b"Third" in data:
                actual_write(data[:-2])
                raise KeyboardInterrupt
            return actual_write(data)

        with (
            patch.object(file, "write", side_effect=interrupted_write),
            self.assertRaises(KeyboardInterrupt),
        ):
            report.append({"release": "Third"})
        self.assertEqual(
            [{"release": "First"}, {"release": "Second"}], json.loads(file.getvalue())
        )

    def test_partial_requires_matching_audio_and_exact_reports_missing_same_edition_formats(
        self,
    ):
        audio = ("FLAC", "24bit Lossless")
        matches = {
            "correct-audio": app.OpsMatch(1, audio, ("catalogue number differs",)),
            "wrong-encoding": app.OpsMatch(
                1, ("FLAC", "Lossless"), ("catalogue number differs",)
            ),
            "wrong-format": app.OpsMatch(1, ("WAV", "24bit Lossless"), ()),
            "mp3-320": app.OpsMatch(1, ("MP3", "320"), ()),
            "other-group-v0": app.OpsMatch(2, ("MP3", "V0 (VBR)"), ()),
        }
        exact, partial = BytesIO(), BytesIO()
        reports = {
            app.EXACT_REPORT: app.JsonReport(exact),
            app.PARTIAL_REPORT: app.JsonReport(partial),
        }
        self.assertEqual(
            app.PARTIAL_REPORT, app.write_search_result(reports, {}, matches, audio)
        )
        (partial_row,) = json.loads(partial.getvalue())
        self.assertEqual(["correct-audio"], partial_row["ops_urls"])
        self.assertNotIn("missing_ops_formats", partial_row)
        matches["exact"] = app.OpsMatch(1, audio, ())
        with redirect_stdout(StringIO()):
            self.assertEqual(
                app.EXACT_REPORT, app.write_search_result(reports, {}, matches, audio)
            )
        (exact_row,) = json.loads(exact.getvalue())
        self.assertEqual(["exact"], exact_row["ops_urls"])
        self.assertEqual(
            [
                {"format": "MP3", "encoding": "V0 (VBR)"},
                {"format": "FLAC", "encoding": "Lossless"},
            ],
            exact_row["missing_ops_formats"],
        )
        self.assertEqual([partial_row], json.loads(partial.getvalue()))

    def test_partial_file_verification_checks_only_equal_totals_inline_and_from_saved_reports(
        self,
    ):
        artists = [{"name": "The Artist"}]
        red_torrent = torrent_edition(
            id=42,
            media="Blu-Ray",
            remasterTitle="40th Anniversary 5.1 Mix",
            fileCount=2,
            size=300,
            fileList="Disc/track.flac{{{275}}}|||cover.png{{{25}}}",
        )
        group = {"id": 5, "name": "Album", "musicInfo": {"artists": artists}}
        ops_edition = {
            "media": "BD",
            "remasterTitle": "40th Anniversary Edition / 5.1 Multichannel",
        }
        ops_torrents = [
            torrent_edition(id=90, fileCount=2, size=300, **ops_edition),
            torrent_edition(id=91, fileCount=2, size=300, **ops_edition),
            torrent_edition(id=92, fileCount=3, size=300, **ops_edition),
            torrent_edition(id=93, fileCount=2, size=400, **ops_edition),
            torrent_edition(
                id=94,
                format="MP3",
                encoding="320",
                fileCount=2,
                size=300,
                **ops_edition,
            ),
            torrent_edition(id=95, media="DVD", fileCount=2, size=300),
        ]
        ops_lists = {
            90: "cover.png{{{25}}}|||Disc/track.flac{{{275}}}",
            91: "cover.png{{{24}}}|||Disc/track.flac{{{276}}}",
        }

        def ops_request(action, **params):
            if action == "artist":
                return {
                    "torrentgroup": [
                        {
                            "groupId": 8,
                            "groupName": "Album",
                            "artists": artists,
                            "torrent": ops_torrents,
                        }
                    ]
                }
            if action == "download":
                self.assertEqual(90, params["id"])
                return TORRENT_BODY
            self.assertEqual("torrent", action)
            torrent_id = params["id"]
            return {
                "torrent": {
                    **next(row for row in ops_torrents if row["id"] == torrent_id),
                    "fileList": ops_lists[torrent_id],
                }
            }

        for inline in (False, True):
            with (
                self.subTest(inline=inline),
                tempfile.TemporaryDirectory() as tmp,
                redirect_stdout(StringIO()),
            ):
                qbit, red, ops = Mock(), Mock(blocked=""), Mock(blocked="")
                qbit.get.side_effect = [
                    [
                        {
                            "hash": "a" * 40,
                            "name": "Album",
                            "content_path": "M:/Music/Album",
                        }
                    ],
                    {"comment": "https://redacted.sh/torrents.php?torrentid=42"},
                ]
                red.get.return_value = {"group": group, "torrent": red_torrent}
                ops.get.side_effect = ops_request
                output_dir = Path(tmp)
                downloads = output_dir / "downloads"
                self.assertEqual(
                    0,
                    app.scan(
                        qbit,
                        red,
                        ops,
                        output_dir,
                        limit=1,
                        rescan=False,
                        verify_partial_files=inline,
                        download_ops_torrents=downloads,
                    ),
                )
                (partial_path,) = output_dir.glob("*/ops-partial-matches.json")
                if not inline:
                    ops.get.assert_called_once_with(
                        "artist", artistname="The Artist", artistreleases=1
                    )
                    (saved,) = json.loads(partial_path.read_text(encoding="utf-8"))
                    self.assertEqual(4, len(saved["ops_urls"]))
                    self.assertEqual(
                        ["edition title differs"],
                        saved["match_exceptions"][app.site_link(app.OPS, 8, 90)],
                    )
                    self.assertEqual(2, len(saved["file_check"]["candidates"]))
                    self.assertNotIn(
                        "Disc/track.flac", partial_path.read_text(encoding="utf-8")
                    )
                    with (
                        patch.object(app, "Gazelle", return_value=ops),
                        patch.object(
                            app,
                            "QBit",
                            side_effect=AssertionError(
                                "saved mode contacted qBittorrent"
                            ),
                        ),
                    ):
                        self.assertEqual(
                            0,
                            app.main(
                                [
                                    "--verify-partial-files",
                                    "--ops-api-key",
                                    "test-key",
                                    "--output-dir",
                                    tmp,
                                ]
                            ),
                        )
                self.assertEqual(
                    [90, 91],
                    [
                        call.kwargs["id"]
                        for call in ops.get.call_args_list
                        if call.args == ("torrent",)
                    ],
                )
                self.assertEqual(
                    [], json.loads(partial_path.read_text(encoding="utf-8"))
                )
                (exact,) = json.loads(
                    partial_path.with_name("ops-exact-matches.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual([app.site_link(app.OPS, 8, 90)], exact["ops_urls"])
                self.assertEqual("file_list", exact["match_basis"])
                if inline:
                    self.assertEqual(
                        TORRENT_BODY, (downloads / "Album.torrent").read_bytes()
                    )
                else:
                    self.assertEqual([], list(downloads.iterdir()))
                self.assertEqual(
                    [{"format": "MP3", "encoding": "V0 (VBR)"}],
                    exact["missing_ops_formats"],
                )
                ops.get.reset_mock()
                self.assertEqual(
                    0, app.verify_saved_partial_files(ops, output_dir, limit=None)
                )
                ops.get.assert_not_called()

    def test_scan_appends_to_existing_reports_and_rotates_only_on_request(self):
        hashes = [digit * 40 for digit in "1234"]
        artists = [{"name": "Artist"}]
        qbit, red, ops = Mock(), Mock(), Mock()

        def webui(endpoint, expected, **params):
            if endpoint == "info":
                return [
                    {
                        "hash": value,
                        "name": "Album",
                        "content_path": f"M:/Music/{value[0]}",
                    }
                    for value in hashes
                ]
            if endpoint == "trackers":
                return [{"url": "https://flacsfor.me/announce"}]
            self.assertEqual("properties", endpoint)
            return {
                "comment": f"https://redacted.sh/torrents.php?torrentid={hashes.index(params['hash']) + 1}"
            }

        qbit.get.side_effect = webui
        red.get.side_effect = lambda action, id: {
            "group": {"id": 5, "name": "Album", "musicInfo": {"artists": artists}},
            "torrent": torrent_edition(id=id, format="FLAC", encoding="Lossless"),
        }
        ops.get.return_value = {
            "torrentgroup": [
                {
                    "groupId": 9,
                    "groupName": "Album",
                    "artists": artists,
                    "torrent": [
                        torrent_edition(id=9, format="FLAC", encoding="Lossless")
                    ],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(StringIO()):
            output_dir = Path(tmp)
            previous_report = output_dir / "20260101T000000.000000Z"
            previous_report.mkdir()
            match_file = previous_report / "ops-exact-matches.json"
            match_file.write_text(
                json.dumps(
                    [{"torrent_hash": hashes[0], "release_name": "Original row"}]
                )
                + " " * 2048,
                encoding="utf-8",
            )

            for new_reports in (False, True, False):
                self.assertEqual(
                    0,
                    app.scan(
                        qbit,
                        red,
                        ops,
                        output_dir,
                        limit=1,
                        rescan=False,
                        new_reports=new_reports,
                    ),
                )

            first, second = sorted(
                directory
                for directory in output_dir.iterdir()
                if app.REPORT_DIRECTORY.fullmatch(directory.name)
            )
            self.assertEqual(previous_report, first)
            for directory, expected in ((first, hashes[:2]), (second, hashes[2:])):
                rows = json.loads(
                    (directory / "ops-exact-matches.json").read_text(encoding="utf-8")
                )
                self.assertEqual(expected, [row["torrent_hash"] for row in rows])
                self.assertEqual(
                    [],
                    json.loads(
                        (directory / "ops-not-found.json").read_text(encoding="utf-8")
                    ),
                )
            self.assertEqual(
                "Original row",
                json.loads(match_file.read_text(encoding="utf-8"))[0]["release_name"],
            )

    def test_report_lock_excludes_another_scan_and_releases_on_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with (
                app.lock_reports(directory),
                self.assertRaises(app.ScanError),
                app.lock_reports(directory),
            ):
                self.fail("A second scan acquired the same report lock")
            with app.lock_reports(directory):
                pass

    def test_label_matching_requires_whole_words_when_both_labels_are_present(self):
        source = app.Edition.from_api(
            {}, torrent_edition(remasterRecordLabel="Virgin EMI")
        )
        for label, expected in (
            ("EMI", True),
            ("Virgin / EMI", True),
            ("Emi", True),
            ("Gemini", False),
            ("EM", False),
            ("", True),
        ):
            with self.subTest(label=label):
                target = app.Edition.from_api(
                    {}, torrent_edition(remasterRecordLabel=label)
                )
                self.assertIs(source.compare(target), expected)
                self.assertIs(target.compare(source), expected)

    def test_web_release_matches_with_one_sided_catalogue_number_and_edition_title(
        self,
    ):
        source = app.Edition(2016, "Virgin EMI", "", "2016 EMI Remaster", "WEB")
        candidate = torrent_edition(
            id=42,
            media="WEB",
            remasterYear=2016,
            remasterRecordLabel="EMI",
            remasterCatalogueNumber="DIGITAL 123",
            remasterTitle="",
        )
        groups = [
            {
                "groupName": "Album",
                "groupId": 8,
                "artists": [{"name": "The Artist"}],
                "torrent": [candidate],
            }
        ]
        details = Mock()
        self.assertIn(
            f"{app.OPS}/torrents.php?id=8&torrentid=42#torrent42",
            app.ops_matches(
                groups, details, "Album", ("The Artist",), source, ("FLAC", "Lossless")
            ),
        )
        details.assert_not_called()

        self.assertIs(
            source.compare(app.Edition(2016, "EMI", "", "Different Remaster", "WEB")),
            True,
        )
        self.assertIs(
            source.compare(app.Edition(2016, "", "DIGITAL 123", "", "WEB")), True
        )
        self.assertIs(
            app.Edition(2016, "Virgin EMI", "DIGITAL 456", "", "WEB").compare(
                app.Edition.from_api({}, candidate)
            ),
            True,
        )
        self.assertIs(
            app.Edition(2016, "Virgin EMI", "", "", "CD").compare(
                app.Edition(2016, "EMI", "CD 123", "", "CD")
            ),
            True,
        )


if __name__ == "__main__":
    unittest.main()
