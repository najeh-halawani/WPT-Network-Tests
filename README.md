# WPT network-emitting tests

The [Web Platform Tests](https://web-platform-tests.org/) (WPT) contain ~37,000
runnable tests. Only some of them make the browser send a network request
beyond loading the test page itself. This kit finds those tests, proves each
one by checking **wptserve's access log**, and packages them as a runnable WPT
tree you can `wpt serve` / `wpt run` directly.

Nothing here is LNA-specific: stock WPT tests, stock headless Chrome flags,
no shims and no retargeting.

| you want to… | read / run |
|---|---|
| run the tests | [HOW-TO-RUN.md](HOW-TO-RUN.md) |
| see where the tests are and how the manifest is laid out | [TREE.md](TREE.md) |
| the list of emitting tests | [`data/census.txt`](data/) (one URL per line) |
| per-test evidence (what the server logged) | `data/census.json` |
| check one test yourself | `python -m netcensus verify <test>` |

## What "emits network" means here

A test counts as emitting when **wptserve's access log holds at least one
request the test caused, other than loading the test itself.**

Every WPT test is served over HTTP, so the test document and the harness
(`testharness.js`, `testharnessreport.js`, `testdriver*.js`, `favicon.ico`, and
for generated tests, the `foo.any.js` / `foo.any.worker.js` wrapper scripts)
appear in the log for *every* test. Those are filtered out by name
([`netcensus/accesslog.py`](netcensus/accesslog.py), `HARNESS_PATHS` and
`OwnDocs`). Whatever remains is the test's own traffic: a `fetch()`, an XHR, an
`<img>`, an iframe, a worker's import, a redirect chain, and so on.

Why the server, not the source code: a test that *mentions* `fetch()` is not a
test that *sends* a request. One example is
`fetch/api/basic/request-head.any.js`. It calls `fetch(".", {method: "HEAD", body: "test"})`,
which is rejected with a `TypeError` before any request is made. A regex scan
lists it as a fetch test. The access log shows that it sends nothing.

## How a test is checked

```
 MANIFEST.json ──► every runnable test URL (testharness, reftest, print-reftest, crashtest)
                       │
                       ▼
 headless Chrome ──► open the test in a fresh tab, wait for testharness to report,
 (over CDP)          keep watching 1.5 s for late requests
                       │                                  │
                       ▼                                  ▼
          wpt serve --verbose access log           browser's own request list (CDP)
          lines attributed by Referer              (context only)
                       │                                  │
                       └──────────────► classify ◄────────┘
                                          │
                       emitted  ·  no network  ·  attempted-but-never-served
```

The two sources answer different questions:

* **Access log**: did the request reach the server? This alone decides
  `emitted`.
* **Browser request list (CDP)**: did the browser try? This is reported as
  `cdp_only` when the browser made an attempt the server never saw (blocked,
  served from cache, or sent to a host other than the WPT server). Those tests
  are kept out of the list but recorded in the JSON, because they are often
  interesting in their own right.

### Attribution, and its known limit

Each log line is credited to the test named in its `Referer`. Lines arrive
from many tests at once, and attribution still works because it does not
depend on timing. It covers requests made from inside a test's own workers,
because their Referer is the test's wrapper script. To keep that
unambiguous, all variants of one source file (`foo.any.html`,
`foo.any.worker.html`, …) run back to back on the same worker.

Two kinds of line cannot be tied to a single test: lines with **no Referer**
(fetches made by the browser process itself) and requests from a **nested
document** (an iframe's own subresources, whose Referer is the iframe). These
are counted as `orphan_requests` and never credited to any test. So a parallel
census can **under-count** a test's traffic, but it can never invent traffic.
`verify` runs tests one at a time and prints every line, including these.

## Layout

```
network-tests/
├── README.md              this file
├── HOW-TO-RUN.md          wpt serve, wpt run, the census, verify
├── TREE.md                test tree + manifest tree (generated: netcensus tree)
├── wpt_serve_config.json  wptserve ports used by the census
├── netcensus/             the tool  (python -m netcensus …)
│   ├── cli.py             subcommands: list, census, verify, subtree, tree
│   ├── config.py          paths, ports, defaults (stated once)
│   ├── manifest.py        reads MANIFEST.json → test URLs; URL → source file
│   ├── server.py          wpt serve --verbose lifecycle, port safety
│   ├── browser.py         headless Chrome over CDP, one per worker
│   ├── cdp.py             minimal DevTools client
│   ├── accesslog.py       access-log follower: parse, pair, attribute, filter noise
│   ├── classify.py        THE decision: emitted / no network / cdp_only
│   ├── runner.py          parallel census, grouping, checkpoint + resume
│   ├── verify.py          serial check with full colored evidence
│   ├── subtree.py         build the pruned runnable WPT tree
│   ├── tree.py            render TREE.md
│   ├── console.py         colored per-test output
│   └── proc.py            process-tree / port helpers (Windows + POSIX)
├── tests/                 unit tests (no browser, no server)
├── data/                  census output: census.json, census.jsonl, census.txt
└── wpt-network/           generated, runnable WPT tree of emitting tests (gitignored)
```

## Requirements

* Python ≥ 3.10 with `websockets` ≥ 12 (`pip install -r requirements.txt`)
* A WPT checkout with `MANIFEST.json` (default `../../wpt`, override with `--wpt` / `WPT_ROOT`)
* Chrome or Chromium (default `../../browsers/chrome-win64/chrome.exe`, override with `--chrome` / `WPT_CHROME`)

Run the unit tests with `python -m unittest discover -s tests -v`.
