# How to run

All commands are run from `network-tests/` unless they say otherwise. Shell
examples are given for PowerShell (Windows) and bash (Linux/macOS).

## 0. One-time setup

```sh
pip install -r requirements.txt          # websockets
```

WPT serves everything from `web-platform.test` and its subdomains, so those
names must resolve to `127.0.0.1`. The census does this inside Chrome (with
`--host-resolver-rules`). For `wpt serve` in your own browser, or for
`wpt run`, add them to the system hosts file once:

```powershell
# Windows, in an *Administrator* PowerShell, from the WPT checkout
python wpt make-hosts-file | Out-File $env:SystemRoot\System32\drivers\etc\hosts -Encoding ascii -Append
```
```sh
# Linux / macOS
./wpt make-hosts-file | sudo tee -a /etc/hosts
```

## 1. Build the runnable tree of emitting tests

```sh
python -m netcensus subtree data/census.json --out wpt-network
```

This produces `wpt-network/`: a real WPT checkout that contains **only** the
emitting tests, at their original paths. It also includes everything they
need: the `wpt` CLI, `tools/`, `resources/`, `common/`, `interfaces/`, shared
media, and every support file in each directory involved, plus its own fresh
`MANIFEST.json` and `NETWORK-TESTS.txt`. Tests that do not emit are left out.
It takes a few minutes, mostly for `wpt manifest`.

## 2. `wpt serve`: browse the tests by hand

```sh
cd wpt-network
python wpt serve                 # add --verbose to see the access log live
```

Open <http://web-platform.test:8000/>, for example
<http://web-platform.test:8000/xhr/send-redirect.htm>. HTTPS tests
(`*.https.*`) are on <https://web-platform.test:8443/>. Stop the server with
Ctrl-C.

With `--verbose`, wptserve prints one line per request:

```
[… http on port 8000] DEBUG - GET /xhr/resources/content.py
[… http on port 8000] DEBUG - 200 GET /xhr/resources/content.py (b'http://web-platform.test:8000/xhr/send-redirect.htm') 57
                              ↑status  ↑path the browser asked for   ↑Referer = the page that asked
```

These are the lines the census reads.

## 3. `wpt run`: run the tests with WPT's own runner

```sh
cd wpt-network
# every emitting test, headless, chromedriver fetched to match the binary
python wpt run chrome --binary ../../../browsers/chrome-win64/chrome.exe \
    --install-webdriver --yes --headless \
    --include-file NETWORK-TESTS.txt \
    --log-wptreport ../data/wptreport.json --log-mach -

# one directory or one file
python wpt run chrome --binary … --install-webdriver --yes --headless fetch/api/basic
python wpt run chrome --binary … --install-webdriver --yes --headless xhr/send-redirect.htm
```

| flag | meaning |
|---|---|
| `chrome` / `firefox` / `chromium` | product; `--binary` points at the browser |
| `--install-webdriver` | download a chromedriver/geckodriver matching `--binary` (or pass `--webdriver-binary`) |
| `--include-file F` | run exactly the test URLs listed in `F` (one per line) |
| `--processes N` | parallel browser instances |
| `--log-wptreport F` | machine-readable results (pass/fail per subtest) |
| `--no-manifest-update` | skip re-checking `MANIFEST.json` (faster when nothing changed) |

`wpt run` reports pass/fail. It **does not** show the access log: wptrunner
pins the server's logger to INFO, which drops the per-request lines. Use
step 4 for the network evidence.

## 4. Check that a test emits: `verify`

```sh
python -m netcensus verify xhr/send-redirect.htm
python -m netcensus verify fetch/api/basic/request-head.any.js     # all variants of a source
python -m netcensus verify fetch/api/basic/                       # a whole directory
python -m netcensus verify --hide-noise xhr/send-redirect.htm     # only the test's own lines
```

`verify` runs serially, so every logged line belongs to the test being run,
and prints the evidence in color:

```
[1/1] ▶ testing  /xhr/send-redirect.htm
    ✔ network emits  46 request(s)
        noise  200  GET  :8000/xhr/send-redirect.htm            ← the test page (grey)
        noise  200  GET  :8000/resources/testharness.js         ← harness (grey)
        own    200  POST :8000/xhr/resources/content.py         ← counted (green)
        own    301  GET  :8000/xhr/resources/redirect.py?…       ← counted (green)
        other  …                                                ← no attributable Referer (yellow)
```

| result line | meaning |
|---|---|
| `✔ network emits` (green) | the access log has at least one request of the test's own |
| `· no network` (grey) | only the page and the harness were logged |
| `⚠ attempted, never reached server` (yellow) | the browser tried (CDP saw it), and the server never logged it |
| `✖ error` (red) | the browser or the CDP session failed for this test |
| `[harness did not report]` (magenta) | testharness never completed within `--timeout` |

## 5. Re-run the census (regenerate the list)

```sh
python -m netcensus census -j 8                         # whole tree → data/census.{json,jsonl,txt}
python -m netcensus census -j 8 --resume                # continue after a crash or Ctrl-C
python -m netcensus census --filter fetch/ -j 8 --out data/fetch.json
python -m netcensus -q census -j 8                      # print only the emitting tests
python -m netcensus list --filter webrtc/               # just enumerate, no browser
```

Every test prints two lines: `▶ testing <url>`, then its network verdict.
Results are appended to `data/census.jsonl` as each test finishes, so
`--resume` loses nothing. On exit, `census.json` (full rows + summary) and
`census.txt` (emitting URLs) are rebuilt from that file.

Then rebuild the tree and the docs:

```sh
python -m netcensus subtree data/census.json --out wpt-network --force
python -m netcensus tree data/census.json --out TREE.md
```

### Options that change the answer

| option | default | effect |
|---|---|---|
| `--timeout` | 20 s | how long to wait for testharness to report. Shorter misses slow tests' late requests |
| `--settle` | 1.5 s | how long to keep watching after it reports. Catches requests fired after completion |
| `-j / --jobs` | 8 | parallel browsers. Does not change attribution (see README), only speed |
| `--types` | testharness, reftest, print-reftest, crashtest | which manifest types to run |

### Before a run

* The census starts its own `wpt serve` on ports 8000–8446, 8888/8889 and 9000.
  If another process holds any of them, the census **refuses to start** and
  names the PID. It never kills a process it did not start.
* Do not run two censuses, or a census next to a manual `wpt serve`, at the
  same time.

## Per-test JSON row (`data/census.json` → `rows[]`)

```json
{
  "url": "/xhr/send-redirect.htm",
  "type": "testharness",
  "emitted": true,                 // ← the answer
  "n_logged": 46,                  // distinct own requests in the access log
  "logged": ["GET :8000/xhr/resources/content.py", "…"],
  "statuses": [200, 301, 302],     // a 404 still counts: the request reached the server
  "n_cdp": 48,                     // requests the browser attempted (non-noise)
  "cdp_only": false,               // attempted, but nothing reached the server
  "completed": true,               // testharness reported
  "targets": 1,                    // page + frames + workers attached
  "seconds": 1.9
}
```
