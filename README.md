# WPT network-emitting tests

The [Web Platform Tests](https://web-platform-tests.org/) (WPT) contain about
67,500 runnable tests (37.5k testharness, 27.6k reftests, 1.9k crashtests,
0.4k print-reftests). Only some of them make the browser put a request on
the wire beyond loading the test itself. This kit finds those tests and proves
each one with evidence from the far side of the connection, for **every
protocol WPT serves: HTTP(S), HTTP/2, WebSocket, WebTransport and WebRTC**.
It then packages them as a runnable WPT tree you can `wpt serve` /
`wpt run` directly.

### WebSocket, WebTransport and WebRTC: covered, and how

Not just HTTP. All three are checked, each in its own way:

* **WebSocket**: Chrome reports the handshake over DevTools (CDP). We count it
  when the handshake was sent or the WebSocket server answered it (`101`).
  We then compare our count with the WebSocket server's own handshake log.
* **WebTransport**: `wpt serve` also runs an HTTP/3 server (aioquic). We
  count it when Chrome reports the session as **established**, meaning the
  QUIC handshake finished and the server accepted the session.
* **WebRTC**: there is no server, because the two peers connect directly.
  A small script in the page watches every `RTCPeerConnection`. We count it
  when ICE reaches **connected**, meaning a real UDP/TCP packet went out and
  an answer came back.

| you want to… | read / run |
|---|---|
| run the tests | [HOW-TO-RUN.md](HOW-TO-RUN.md) |
| see where the tests are and how the manifest is laid out | [TREE.md](TREE.md) |
| the list of emitting tests | `data/census.txt` (one URL per line) |
| the same list split by protocol | `data/census-{http,websocket,webtransport,webrtc}.txt` |
| per-test evidence | `data/census.json.gz` (gunzip -k it to get `data/census.json`; the raw file is ~260 MB) |
| check one test yourself | `python -m netcensus verify <test>` |

## What counts as "emits network"

Every claim needs evidence from the other side of the connection. That
evidence differs per protocol, because WPT runs a different server for each:

| protocol | server (`wpt serve`) | evidence that a test's request reached it |
|---|---|---|
| HTTP(S), HTTP/2 | wptserve `:8000/:8443/…`, h2 `:9000` | a line in wptserve's **access log**, credited to the test by its `Referer` |
| WebSocket | pywebsocket `:8888/:8889` | the handshake request was written on an established connection, or the server answered it (101). Each run also compares the total with the number of handshakes **the WebSocket server itself logged** |
| WebTransport | aioquic HTTP/3 (UDP, auto port) | the session was **established**: QUIC handshake done and the server accepted the HTTP/3 CONNECT (this server logs no sessions) |
| WebRTC | none: peer to peer | ICE reached **connected**: a STUN connectivity check made a request/response round trip over a real UDP/TCP socket. The selected candidate pair is recorded |

HTTP traffic is split into two tiers, because not every request a test causes
is the test *doing* something:

* **static**: in the markup (`<script src>`, `<img>`, CSS) or a declared
  `// META: script=helper.js` dependency. Loading these is part of loading the
  test, like `testharness.js`.
* **runtime**: issued by running code: `fetch()`, XHR, `sendBeacon`, dynamic
  elements, worker imports, CORS preflights, redirect hops, and anything the
  browser sent on the page's behalf.

WebSocket, WebTransport and WebRTC are always runtime. **`data/census.txt`
lists the tests with runtime evidence**; `census-static.txt` lists the tests
whose only traffic is static.

The test document itself and the harness (`testharness.js`,
`testharnessreport.js`, `testdriver*.js`, `favicon.ico`, and for generated
tests the `foo.any.js` / `foo.any.worker.js` wrapper scripts) are filtered out
by name ([`netcensus/accesslog.py`](netcensus/accesslog.py)).

**Why evidence, not source code.** A test that *mentions* `fetch()` is not a
test that *sends* a request. For example, `fetch/api/basic/request-head.any.js`
calls `fetch(".", {method: "HEAD", body: "test"})`, which is rejected with a
`TypeError` before anything is sent. A regex scan lists it; the access log
shows nothing.

## How a test is checked

```
 MANIFEST.json ──► every runnable test URL, plus its declared META dependencies
                       │
                       ▼
 headless Chrome ──► fresh tab per test; every frame, worker, shared worker and
 (CDP, wptrunner     service worker is attached PAUSED, instrumented, resumed;
  flags)             wait for testharness to report, then 1.5 s more
        │                  │                       │                      │
      HTTP(S), h2        WebSocket               WebTransport           WebRTC
        ▼                  ▼                       ▼                      ▼
  wptserve access log   WebSocket handshake    WebTransport session   WebRTC ICE
  (Referer-attributed)  sent / answered        established            connected
                        (CDP event, checked    (CDP event; QUIC +     (in-page observer;
                        against pywebsocket's  HTTP/3 CONNECT to      STUN round trip,
                        own handshake log)     aioquic accepted)      peer to peer)
        └──────────────────┴───────────┬───────────┴──────────────────────┘
                                       ▼
                     classify:  runtime · static only · no network · attempted-not-served
```

Before the first test, the run checks that **every protocol server actually
started**. It refuses to run if one didn't. A missing server (for example,
WebTransport without `aioquic`) otherwise produces "no network" for that whole
protocol with no error anywhere.

### Attribution, and its known limits

* HTTP log lines are credited by `Referer`, so tests can run in parallel
  without mixing. Requests made inside a test's workers carry the worker
  script as Referer, and are credited to the test. All variants of one source
  file run on the same worker, so this is never ambiguous.
* Lines with **no Referer** (browser-process fetches) or from a **nested
  document** (an iframe's own subresources) cannot be credited to a single
  test. They are counted as `orphan_requests` and never charged to anyone. A
  parallel census can therefore **under-count** a test's traffic, never invent
  any. `verify` runs serially and prints every line.
* WebSocket over HTTP/2 (`?wpt_flags=h2` variants) does not complete in this
  configuration: the h2 server logs no handshakes for them, and they score
  as no WebSocket traffic, which is what happened on the wire.
* WebRTC evidence comes from a passive observer in the page. It wraps
  `setLocalDescription`/`setRemoteDescription` to watch ICE state and
  deliberately leaves `RTCPeerConnection` itself untouched.

## Layout

```
network-tests/
├── README.md              this file
├── HOW-TO-RUN.md          setup, wpt serve, wpt run, census, verify, large runs
├── TREE.md                test tree + manifest tree (generated: netcensus tree)
├── requirements.txt       websockets, aioquic (WebTransport server)
├── wpt_serve_config.json  wptserve ports used by the census
├── netcensus/             the tool  (python -m netcensus …)
│   ├── cli.py             subcommands: list, census, verify, subtree, tree
│   ├── config.py          paths, ports, defaults (stated once)
│   ├── manifest.py        MANIFEST.json → test URLs + META deps; URL → source file
│   ├── server.py          wpt serve lifecycle, per-protocol preflight, port safety
│   ├── browser.py         headless Chrome over CDP: wptrunner flags, target
│   │                      cascade, WebSocket/WebTransport events, WebRTC observer
│   ├── cdp.py             minimal DevTools client
│   ├── accesslog.py       access-log follower: parse, pair, attribute, filter noise,
│   │                      count WebSocket-server handshakes
│   ├── classify.py        THE decision: per-protocol evidence, runtime vs static
│   ├── runner.py          parallel census, grouping, checkpoint/resume, session ledger
│   ├── verify.py          serial check with full colored evidence
│   ├── subtree.py         build the pruned runnable WPT tree
│   ├── tree.py            render TREE.md
│   ├── console.py         colored per-test output and run summary
│   └── proc.py            process trees, ports; children die with the run
├── tests/                 unit tests (no browser, no server)
├── data/                  census output (see HOW-TO-RUN.md)
└── wpt-network/           generated runnable WPT tree of emitting tests (gitignored)
```

## Requirements

* Python ≥ 3.10, then `pip install -r requirements.txt`
* A WPT checkout with `MANIFEST.json` (default `../../wpt`, or `--wpt` / `WPT_ROOT`)
* Chrome or Chromium (default `../../browsers/chrome-win64/chrome.exe`, or `--chrome` / `WPT_CHROME`)

Run the unit tests with `python -m unittest discover -s tests -v`.
