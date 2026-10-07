# How to run

Run every command from the repository root (`WPT-Network-Tests/`) unless it says otherwise. On **Git
Bash**, write test paths without a leading `/` (`xhr/send-redirect.htm`, not
`/xhr/send-redirect.htm`). Git Bash rewrites a leading `/` into a Windows
path.

## 0. One-time setup

```sh
pip install -r requirements.txt     # websockets + aioquic (the WebTransport server)
python -m unittest discover -s tests # 54 unit tests, no browser needed
```

WPT serves everything from `web-platform.test` and its subdomains. The
census resolves those inside Chrome, so it needs no hosts file. For `wpt serve`
in your own browser, or for `wpt run`, add them to the system hosts file once:

```powershell
# Windows, *Administrator* PowerShell, inside the WPT checkout
python wpt make-hosts-file | Out-File $env:SystemRoot\System32\drivers\etc\hosts -Encoding ascii -Append
```
```sh
./wpt make-hosts-file | sudo tee -a /etc/hosts      # Linux / macOS
```

## 1. Check a few tests: `verify` (start here)

```sh
python -m netcensus verify xhr/send-redirect.htm
python -m netcensus verify webtransport/datagram-bad-chunk.https.any.js   # all 4 globals
python -m netcensus verify webrtc/RTCDataChannel-send.html
python -m netcensus verify websockets/                                   # a whole directory
python -m netcensus verify --hide-noise fetch/api/basic/                 # only the test's own lines
```

`verify` runs tests **one at a time**. Every line the servers logged during a
test therefore belongs to that test, and is printed in color:

```
• servers  ✔ http  ✔ https  ✔ websocket  ✔ websocket-tls  ✔ h2  ✔ webtransport
[1/1] ▶ testing  /webtransport/datagram-bad-chunk.https.any.html
    ✔ network emits [webtransport]  1 runtime request(s) + 2 static
        runtime  ok   WT  :58982/webtransport/handlers/echo.py  (session established with the h3 server)
        static   200  GET :8443/common/utils.js                  ← markup / META dependency (cyan)
        noise    200  GET :8443/resources/testharness.js         ← harness (grey, hidden by --hide-noise)
        other    …                                               ← logged with no attributable Referer (yellow)
```

| result line | meaning |
|---|---|
| `✔ network emits [http] [websocket] …` (green) | runtime evidence; the tags name the protocols |
| `◦ static subresources only` (cyan) | only markup subresources / META dependencies reached the server |
| `· no network` (grey) | only the page and the harness were logged |
| `⚠ attempted, never reached server` (yellow) | the browser tried, and no server logged or answered it |
| `✖ error` (red) | the browser or CDP failed for this test |
| `[harness did not report]` (magenta) | testharness did not finish within `--timeout` |
| `⚠ ws server logged N handshake(s), M credited` | a WebSocket the census did not see (verify only) |

## 2. Large runs: the census

A census runs many tests in parallel and records a verdict per test. The
whole tree is **67,515 tests, roughly 4–5 hours at `-j 12`** on a 24-core
machine with about 6 GB free RAM. Use `-j 8` on smaller machines. Run it
yourself in a terminal you can leave open:

```powershell
# PowerShell (Windows). Progress shows on screen AND goes to data\census.log
cd WPT-Network-Tests
python -m netcensus census -j 12 --out data\census.json 2>&1 | Tee-Object -FilePath data\census.log
```
```sh
# bash / Git Bash / Linux
python -m netcensus census -j 12 --out data/census.json 2>&1 | tee data/census.log
```

**If it stops** (closed terminal, reboot, Ctrl-C), run the same command with
`--resume`. Every finished test is already saved in `data/census.jsonl`, so
nothing is lost:

```powershell
python -m netcensus census -j 12 --out data\census.json --resume 2>&1 | Tee-Object -FilePath data\census.log -Append
```

**Watching progress.** Every test prints two lines (`▶ testing <url>`, then
its verdict). Every 250 tests a summary line follows:

```
── progress 2500/67515 (3.7%)  250 tests/min  elapsed 0.2h  eta 4.3h (~19:05)  runtime=900 static=700 errors=1 ──
```

From a second terminal:

```powershell
Get-Content data\census.log -Wait -Tail 20                       # live tail
Select-String "── progress" data\census.log | Select-Object -Last 1 # latest progress line only
(Get-Content data\census.jsonl | Measure-Object -Line).Lines      # tests decided so far
```
```sh
tail -f data/census.log
grep "── progress" data/census.log | tail -1
wc -l data/census.jsonl
```

Add `-q` (`python -m netcensus -q census …`) to print only the emitting tests
plus the progress lines.

**On macOS / Linux** (e.g. the Mac mini), `run-full.sh` does all of the
above in one command: it sources `../env.sh` if present, uses the pinned
Chrome, appends to `data/census.log`, and **resumes automatically** when
`data/census.jsonl` already has rows. Run it inside `screen`, so a dropped
ssh session doesn't stop the run:

```sh
screen -S census                 # detach: Ctrl-A then D;  re-attach: screen -r census
cd WPT-Network-Tests && ./run-full.sh
tail -f data/census.log          # from any other terminal
```

The code is the same on every platform; only `WPT_ROOT` / `WPT_CHROME` differ.
One difference to know: macOS has no way to make child processes die with
their parent. A **hard-killed** run (not Ctrl-C) can leave Chrome or
`wpt serve` holding the ports, and the next run will then refuse to start and
name the PID.

**Smaller runs** for a protocol or a directory (minutes, not hours):

```sh
python -m netcensus census --filter "webrtc/,webtransport/" -j 8 --out data/rtc-wt.json
python -m netcensus census --filter websockets/ -j 8 --out data/websockets.json
python -m netcensus census --filter fetch/ -j 12 --out data/fetch.json
python -m netcensus list --filter webrtc/          # just enumerate, no browser
```

### What a census writes

| file | contents |
|---|---|
| `data/census.txt` | **tests with runtime evidence**, the headline list (one URL per line) |
| `data/census-http.txt`, `-websocket.txt`, `-webtransport.txt`, `-webrtc.txt` | the same list split by protocol (a test can be in several) |
| `data/census-static.txt` | tests whose only traffic is markup / META dependencies |
| `data/census.json` | summary + one row per test with its evidence (format below) |
| `data/census.jsonl` | checkpoint, one row per line as tests finish (`--resume` reads it) |
| `data/census.sessions.jsonl` | per-session run-level counts (orphans, WebSocket server cross-check, server status) |

The end-of-run summary:

```
tests run                   67515
✔ emit at runtime           …
◦ static subresources only  …
· no network                …
⚠ attempted, not served     …
✖ errors                    …
unattributed log lines      …  (never charged to a test)
websocket handshakes        N credited / M logged by the ws server
── runtime emitters by protocol ──
  [http] … [websocket] … [webtransport] … [webrtc] …
```

`websocket handshakes` is a cross-check: equal numbers mean every handshake
the WebSocket server saw was credited to a test.

### Before a run

* The census starts its own `wpt serve` on 8000–8446, 8888/8889, 9000 and a
  UDP port for WebTransport. If another process holds one of those ports, it
  **refuses to start** and names the PID. It never kills a process it did not
  start.
* It then checks that **every protocol server is up** (`• servers ✔ http …`)
  and refuses to run if one is not, because those tests would silently score
  "no network". `--allow-missing-servers` overrides this, and the gap is
  recorded in `servers_down`.
* Don't run two censuses, or a census alongside your own `wpt serve`, at the
  same time.
* Child processes (Chrome, wpt serve) die with the census however it ends, so
  a killed run leaves nothing holding the ports.

### Options that change the answer

| option | default | effect |
|---|---|---|
| `--timeout` | 20 s | how long to wait for testharness to report |
| `--settle` | 1.5 s | how long to keep watching after it does (late requests) |
| `-j / --jobs` | 8 | parallel browsers; changes speed, not attribution |
| `--types` | testharness, reftest, print-reftest, crashtest | manifest types to run |
| `--filter` | everything | path prefix(es), comma-separated |
| `--serial-recheck` | `webrtc` | after the parallel pass, re-run **serially** the tests under these prefixes that showed no runtime evidence; the serial verdict replaces the parallel one. WebRTC is timing-bound: under parallel load ICE may not connect before the test ends (measured: 3 of 205 emitters lost at `-j 8`, all 3 recovered serially). `""` disables |

## 3. Build the runnable tree of emitting tests

```sh
python -m netcensus subtree data/census.json --out wpt-network            # runtime emitters
python -m netcensus subtree data/census.json --out wpt-network --include-static
python -m netcensus tree data/census.json --out TREE.md                   # regenerate TREE.md
```

`wpt-network/` is a real WPT checkout that contains only the selected tests,
at their original paths. It also includes everything they need: the `wpt`
CLI (including every directory listed in `tools/wpt/paths`), `tools/`,
`resources/`, `common/`, `interfaces/`, shared media, and every support file
of the directories involved. It has its own fresh `MANIFEST.json` and a
`NETWORK-TESTS.txt` list. A source file is kept whole, so its sibling global
variants come along too (205 selected URLs became a 214-URL tree).

To check that the tree is complete, run the census inside it and compare. A
missing support file wouldn't error; it would make a test emit less:

```sh
python -m netcensus census --wpt wpt-network -j 8 --out data/in-tree.json
```

Validated on the 205 WebRTC/WebTransport runtime emitters: all 205 are
present and all 205 emit inside the pruned tree (3 needed the serial
re-check, see `--serial-recheck`).

## 4. `wpt serve`: browse the tests by hand

```sh
cd wpt-network
python wpt serve --webtransport-h3      # add --verbose to watch the access log live
```

Open <http://web-platform.test:8000/> (HTTPS tests: <https://web-platform.test:8443/>).
With `--verbose`, wptserve prints one line per request; these are the lines
the census reads:

```
[… http on port 8000] DEBUG - 200 GET /xhr/resources/content.py (b'http://web-platform.test:8000/xhr/send-redirect.htm') 57
                              ↑status  ↑path requested          ↑Referer: the page that asked
```

## 5. `wpt run`: WPT's own runner (pass/fail)

```sh
cd wpt-network
python wpt run chrome --binary ../../../browsers/chrome-win64/chrome.exe \
    --install-webdriver --yes --headless --enable-webtransport-h3 \
    --include-file NETWORK-TESTS.txt --log-wptreport ../data/wptreport.json --log-mach -

python wpt run chrome --binary … --install-webdriver --yes --headless webrtc/RTCDataChannel-send.html
```

| flag | meaning |
|---|---|
| `--install-webdriver` | download a chromedriver matching `--binary` (or pass `--webdriver-binary`) |
| `--include-file F` | run exactly the test URLs listed in `F` |
| `--processes N` | parallel browsers |
| `--log-wptreport F` | machine-readable pass/fail per subtest |
| `--no-manifest-update` | skip re-checking `MANIFEST.json` |

`wpt run` reports pass/fail but **does not show the access log**: wptrunner
pins the server's logger to INFO. Use `verify` (step 1) for the network
evidence.

## Per-test row (`data/census.json` → `rows[]`)

```json
{
  "url": "/webrtc/RTCDataChannel-send.html",
  "type": "testharness",
  "emitted": true,
  "runtime": true,                          // ← in data/census.txt
  "protocols": ["webrtc"],                  // http | websocket | webtransport | webrtc
  "runtime_requests": ["RTC udp host x.local:58712 -> x.local:58714", "…"],
  "static_requests": ["GET :8000/webrtc/RTCPeerConnection-helper.js"],
  "logged": ["…every own item above, labeled…"],
  "statuses": [200],                        // HTTP statuses; a 404 still reached the server
  "n_ws": 0, "n_wt": 0, "n_rtc": 14,        // per-protocol evidence counts
  "n_cdp": 3,                               // requests the browser attempted
  "cdp_only": false,                        // attempted, nothing reached a server
  "completed": true,                        // testharness reported
  "targets": 1,                             // page + frames + workers attached
  "seconds": 1.4
}
```
