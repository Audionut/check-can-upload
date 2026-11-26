# Check RED releases on OPS

Scan a qBittorrent WebUI for RED torrents, check for matching OPS torrents locally or in the OPS catalogue, and save the results as JSON reports.

## Run on Windows

Enable the qBittorrent WebUI and create read-access API keys on RED and OPS. In PowerShell 7, from this directory, run the following with Python 3.10 or newer installed.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe check_can_upload.py --qbit-url 'http://127.0.0.1:8080' --qbit-username 'admin' --qbit-password (Read-Host 'qBittorrent password' -MaskInput) --red-api-key (Read-Host 'RED API key' -MaskInput) --ops-api-key (Read-Host 'OPS API key' -MaskInput) --limit 50
```

Use `--qbit-api-key` for qBittorrent 5.2 or newer with a WebUI API key. If the WebUI allows unauthenticated access, omit the qBittorrent credentials. Existing environment variables still work as defaults. Explicit qBittorrent credentials take precedence over `QBIT_API_KEY`.

`--limit 50` checks up to 50 new RED torrents, counting unchecked attempts. Omit `--limit` to check all remaining RED torrents. Rerun with the same `--output-dir` to continue. The script reads `torrent_hash` from all JSON reports and legacy CSV reports and skips those torrents before contacting RED or OPS, including saved unchecked entries. Use `--rescan` to check earlier results again. Combine `--rescan` with `--new-reports` to keep the recheck results in their own reports.

Add `--debug` to print each RED API response body to the terminal with its endpoint and HTTP status.

Add `--verify-partial-files` to a normal command with `--qbit-url` to check eligible partial matches as the scan runs. A normal scan saves evidence for later verification without making extra requests. To verify the latest saved partials on their own, run this with the same `--output-dir` used for the scan:

```powershell
python .\check_can_upload.py --verify-partial-files --ops-api-key (Read-Host 'OPS API key' -MaskInput)
```

The saved pass uses only the OPS API key, updates the latest reports in place, and accepts `--limit` to cap the number of releases checked. It looks up an OPS torrent ID only if the saved partial has the same file count and total byte size as RED. Every relative path and individual file size must then agree. The comparison uses tracker file lists. A verified release moves to the exact report with `match_basis` set to `file_list`. Run a normal scan with `--rescan` to collect evidence for partials saved by older versions.

## Reports

Runs append results to the most recent set of JSON reports under `reports` or the directory passed with `--output-dir`. The first run creates a timestamped folder. Use `--new-reports` to start another set in its own timestamped folder. Subsequent runs append to that set and still use report history to skip saved attempts. Each report is a UTF-8 JSON array and is updated as results are saved. Use one scan at a time per output directory.

- `ops-exact-matches.json` contains matches confirmed in qBittorrent or by a tracker lookup. Catalogue matches agree on edition year, record label, catalogue number, edition title, media, format, and encoding. Comparisons allow basic case, whitespace, and catalogue-number formatting differences. Both listings can leave the edition title blank. For exact FLAC catalogue matches, `missing_ops_formats` lists absent MP3 320 and V0 encodings from the same edition. Exact 24-bit FLAC catalogue matches also check for FLAC Lossless.
- `ops-partial-matches.json` contains otherwise compatible OPS links with the same media, format, and encoding. `match_exceptions` lists the allowed edition differences for each link. If a RED torrent has an exact match, the scan writes only its exact links.
- `ops-not-found.json` contains RED releases for which the OPS search completed without finding the same edition. Apply OPS upload rules separately before using this list to choose uploads.
- `ops-unchecked.json` contains identified RED torrents whose check could not finish. Each row includes a `reason` and the release and torrent details available at the time.

Reports include the RED link, artist and release name, qBittorrent torrent name, edition details, and the absolute content path reported by qBittorrent where available. Paths refer to the machine running qBittorrent. A single-file torrent has a file path and a multifile torrent has a root path.

Catalogue matches include arrays of OPS links and formats with encodings and set `same_format_and_encoding` to `true`. Earlier `ops-matches.json` reports still count when resuming. Use `--rescan --new-reports` to produce a fresh set under the current rules.

Before calling RED or OPS, the scan checks qBittorrent torrents with the same name or total byte size. An OPS announce host and identical sets of relative audio filenames and individual byte sizes qualify for `match_basis: qbit_client`. The comparison ignores the top-level folder, artwork, and other non-audio files. These exact matches use only qBittorrent data. `ops_torrents` records the matched torrents' names, hashes, paths, and OPS comment links when available. The catalogue's edition metadata and missing formats require a tracker lookup.

The scan looks for RED links in torrent comments and uses a RED hash lookup when a RED announce or comment identifies a torrent without a usable torrent ID. It looks in the main artist's OPS catalogue and matches releases by album title, main artists, edition year, and media. Releases with more than two main artists or a Various Artists credit are listed as `Various Artists - <title>` and searched by title on OPS. If both sites credit various artists but list different performers, an edition match also requires record labels from both sites or catalogue numbers from both sites.

Title comparison ignores commas, quotation marks, dash styles, and spacing around dashes. A release can match when either site omits the record label, catalogue number, or edition title. When both sites supply a label or edition title, those values must agree. Labels match when one name appears as a whole-word phrase in the other, as in `Virgin EMI` and `EMI`. Catalogue numbers can differ between sites. Audio formats are recorded separately.

The terminal shows the current torrent, its position in the scan, and the edition being searched on OPS. It lists unchecked torrents if a request fails or the available metadata cannot establish whether an edition matches. For a possible match, it includes an OPS link and the missing fields. A run with unchecked torrents exits with code `1`. Ctrl+C keeps the JSON results saved so far and exits with code `130`.

## API pacing

Requests are serial and at least four seconds apart per tracker. Use a higher `--api-interval` for a slower scan. OPS artist, title, and group detail lookups share the same request budget, and title searches observe the lower browse limit. Successful OPS lookups are cached in `.ops-cache` under the output directory for 24 hours. Later scans can check other formats against those results. `--rescan` refreshes the cache as it checks each artist or album. If a tracker throttles it, the scan waits at least 20 seconds and honors a longer `Retry-After` before one retry. If the server requests more than 60 seconds or throttles again, the scan stops and keeps the JSON results saved so far. Avoid overlapping scans or running the scan alongside other heavy use of the same tracker accounts.
