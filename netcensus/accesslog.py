"""The wptserve access log: parse it, attribute lines to a test, drop the noise.

WHY THIS FILE EXISTS
`wpt serve --verbose` raises wptserve to DEBUG, where it writes one line per
request that reached the server.  That log is the only evidence that a test's
request actually went on the wire: a request the browser *made* and a request
the server *answered* are different facts, and only the second one proves the
network was touched.  Everything here turns that log into a per-test answer.

THREE JOBS, IN ORDER

1. PARSE.  wptserve writes two lines per request (wptserve/server.py:348 and
   :379).  The second carries the status and the Referer and is the useful one;
   the first is kept only when no second line followed, because a request that
   arrived and was never finished still arrived.  It also logs its own internal
   path REWRITES, which must be undone or the log names a file the browser
   never asked for.

2. ATTRIBUTE.  The Referer names the document that issued the request, so a
   line belongs to the test whose URL it names -- which holds no matter how
   many tests are in flight, and is why the census can run in parallel at all.
   Lines with NO referer (a browser-process fetch, a top-level navigation)
   cannot be tied to a test this way; they are kept as `orphans` together with
   the set of tests that were running, rather than blamed on one of them.

3. FILTER THE NOISE.  Every WPT test loads over HTTP, so the document itself
   and the testharness boilerplate are always in the log.  Counting those would
   make every test in the tree "emit network" and the answer would be worth
   nothing.  What is left after they are removed is the test's own traffic.

The noise list is deliberately SHORT and NAMED.  Anything broader (say, "all of
/resources/") would swallow real subresource loads -- /resources/ also holds
the images, media and handlers that tests fetch on purpose.
"""
from __future__ import annotations

import collections
import os
import re
import threading
import time

# wptserve's two log lines, and its internal path rewrite.
#   [2026-10-05 14:27:06,496 http on port 8000] DEBUG - GET /fetch/x.html
#   [2026-10-05 14:27:06,498 http on port 8000] DEBUG - 200 GET /fetch/x.html (http://web-platform.test:8000/y.html) 143
RESP_RE = re.compile(
    r"^\[[^\]]*?on port (?P<port>\d+)\]\s+DEBUG\s+-\s+"
    r"(?P<status>\d{3})\s+(?P<method>[A-Z]+)\s+(?P<path>\S+)\s+"
    r"\((?P<referer>.*)\)\s+(?P<length>\d+)\s*$")
REQ_RE = re.compile(
    r"^\[[^\]]*?on port (?P<port>\d+)\]\s+DEBUG\s+-\s+"
    r"(?P<method>GET|POST|HEAD|PUT|DELETE|PATCH|OPTIONS|CONNECT)\s+"
    r"(?P<path>/\S*)\s*$")
# wptserve aliases some paths and logs the TARGET, not what the browser asked
# for (wptserve/server.py:128).  Undo it, or an ordinary helper load looks like
# a request for a file that appears in no page.  Every idlharness test hits it.
REWRITE_RE = re.compile(
    r"^\[[^\]]*?on port \d+\]\s+DEBUG\s+-\s+"
    r"Rewriting request path (?P<src>\S+) to (?P<dst>\S+)\s*$")
# pywebsocket (the ws/wss servers) logs a handshake without its path, Origin
# or Referer, so it cannot be attributed to a test.  It is COUNTED instead:
# "Protocol version is ..." is written exactly once per accepted handshake,
# and the run checks that total against the handshakes it credited to tests.
WS_HANDSHAKE_RE = re.compile(
    r"^\[[^\]]*?\bwss? on port \d+\]\s+DEBUG\s+-\s+Protocol version is ")


# ---------------------------------------------------------------- the noise ---
# Loaded by the harness for every single test, named one by one.  A path is
# noise only if it is in here or is the test document itself.
HARNESS_PATHS = frozenset({
    "/resources/testharness.js",
    "/resources/testharnessreport.js",
    "/resources/testharness.css",
    "/resources/testdriver.js",
    "/resources/testdriver-vendor.js",
    "/resources/testdriver-actions.js",
    "/resources/idlharness.js",
    "/resources/idlharness-shadowrealm.js",
    "/resources/WebIDLParser.js",
    "/resources/webidl2/lib/webidl2.js",      # the alias target of the above
    "/resources/check-layout-th.js",
    "/resources/declarative-shadow-dom-polyfill.js",
    "/favicon.ico",
})
# Prefixes that are harness plumbing rather than a test's own traffic.
HARNESS_PREFIXES = (
    "/resources/testdriver",
    "/tools/",
)


def strip_query(path: str) -> str:
    return path.split("?", 1)[0].split("#", 1)[0]


# The markers WPT's generated-test naming uses (tools/serve/serve.py, the
# WrapperHandler path_replace tables).  `foo.any.js` is served as
# foo.any.html, foo.any.worker.html, foo.any.serviceworker.html, ..., and each
# wrapper loads foo.any.js and/or foo.any.worker.js.
_WRAPPER_MARKERS = (".any.", ".window.", ".worker.", ".extension.", ".test262")


class OwnDocs:
    """The documents and scripts that ARE the test, as opposed to its traffic.

    For an ordinary test that is the test document.  For a generated
    (multi-global) test it is also every wrapper script that shares its stem:
    requests FOR those are part of loading the test (noise), and requests whose
    Referer is one of them were made BY the test -- from inside its worker, for
    instance -- and are attributed to it.
    """

    __slots__ = ("exact", "stem")

    def __init__(self, test_url: str):
        path = strip_query(test_url)
        self.exact = frozenset({test_url, path})
        d, _, base = path.rpartition("/")
        cuts = [base.find(m) for m in _WRAPPER_MARKERS if base.find(m) > 0]
        self.stem = f"{d}/{base[:min(cuts)]}." if cuts else None

    def __contains__(self, path: str) -> bool:
        bare = strip_query(path)
        if path in self.exact or bare in self.exact:
            return True
        if self.stem is None or not bare.startswith(self.stem):
            return False
        rest = bare[len(self.stem) - 1:]          # keeps the leading "."
        return ("/" not in rest and rest.endswith((".js", ".html"))
                and any(rest.startswith(m) for m in _WRAPPER_MARKERS))

    @property
    def group(self) -> str:
        """Tests with the same group share wrapper scripts and must not run
        concurrently, or their worker-issued requests become ambiguous."""
        return self.stem or next(iter(sorted(self.exact)))


def self_paths(test_url: str) -> OwnDocs:
    return OwnDocs(test_url)


def is_noise(path: str, own: OwnDocs) -> bool:
    """Is this logged path the test itself or harness boilerplate, rather than
    something the test asked for?"""
    bare = strip_query(path)
    if path in own:
        return True
    if bare in HARNESS_PATHS:
        return True
    return any(bare.startswith(p) for p in HARNESS_PREFIXES)


def clean_referer(raw: str) -> str:
    """wptserve formats the Referer header with %s on BYTES, so the log holds
    `b'http://...'`.  Undo that; "None" and "" mean no referer."""
    r = raw.strip()
    if len(r) >= 3 and r[0] == "b" and r[1] in "'\"" and r[-1] == r[1]:
        r = r[2:-1]
    return "" if r in ("None", "-", "") else r


def url_path(url: str) -> str:
    """The path of an absolute URL (query and fragment stripped), "" if none."""
    if not url or url in ("None", "-"):
        return ""
    i = url.find("://")
    if i < 0:
        return strip_query(url)
    j = url.find("/", i + 3)
    return strip_query(url[j:]) if j >= 0 else "/"


# A Referer is a URL; the name says which role it plays at the call site.
referer_path = url_path


# ------------------------------------------------------------- the follower ---
class LogRouter:
    """Follows the access log in one thread and sorts lines into per-test bins.

    One reader, not one per worker: wptserve writes from several processes into
    one file, so N readers would each re-parse the same bytes and each would
    have to guess where a half-written line ends.  Reading once, from one
    offset, and holding back a trailing partial line is the only way the parse
    stays honest under concurrency.

    A test is claimed by `open_test(url)` before it is navigated to and
    released by `take(key)` after it settles.  A line no open test can claim
    goes to `orphans()`; it is counted, never charged to a test.

    REQUEST/RESPONSE PAIRING.  wptserve logs `GET /path` when a request
    arrives and `200 GET /path (referer) len` when it is answered.  Only the
    second carries the Referer, so the first is held as PENDING and retired
    by its answer.  A request still pending after `unanswered_after` seconds
    arrived and was never answered -- it did reach the server, so it is
    reported as an unanswered orphan rather than silently dropped.
    """

    def __init__(self, path: str, poll: float = 0.25,
                 unanswered_after: float = 30.0):
        self.path = path
        self.poll = poll
        self.unanswered_after = unanswered_after
        self._pending: dict[tuple, list] = {}  # (port, method, path) -> FIFO
        self.pos = 0
        self._lock = threading.Lock()
        self._bins: dict[str, tuple] = {}      # test path -> (OwnDocs, records)
        # Bounded: a whole-tree run produces orphans by the hundred thousand,
        # and only the count is needed run-wide.  `orphan_total` is exact.
        self._orphans: collections.deque = collections.deque(maxlen=50000)
        self.orphan_total = 0
        self.ws_handshakes = 0                 # counted, see WS_HANDSHAKE_RE
        self._aliases: dict[str, str] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> None:
        # Start from the END of whatever is already there: lines written while
        # the server was coming up belong to no test.
        try:
            self.pos = os.path.getsize(self.path)
        except OSError:
            self.pos = 0
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._drain()
            self._stop.wait(self.poll)
        self._drain()

    # -- per-test bins --------------------------------------------------------
    def open_test(self, test_url: str) -> str:
        """Start collecting for one test; returns the key to take() it by."""
        own = OwnDocs(test_url)
        key = strip_query(test_url)
        with self._lock:
            self._bins[key] = (own, [])
        return key

    def take(self, key: str) -> list:
        """Everything attributed to this test, and close its bin."""
        self._drain()
        with self._lock:
            return self._bins.pop(key, (None, []))[1]

    def orphans(self, flush_pending: bool = False) -> list:
        """Lines no test could claim, since the last call.

        Includes requests that were never answered within `unanswered_after`
        seconds (all still-pending ones when `flush_pending`).
        """
        self._drain()
        cutoff = float("inf") if flush_pending else             time.time() - self.unanswered_after
        with self._lock:
            for k in list(self._pending):
                keep = [r for r in self._pending[k] if r["t"] > cutoff]
                old = [r for r in self._pending[k] if r["t"] <= cutoff]
                self._orphans.extend(old)
                self.orphan_total += len(old)
                if keep:
                    self._pending[k] = keep
                else:
                    del self._pending[k]
            out = list(self._orphans)
            self._orphans.clear()
            return out

    # -- the parse ------------------------------------------------------------
    def _drain(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
                fh.seek(self.pos)
                data = fh.read()
                end = fh.tell()
        except OSError:
            return
        if not data:
            return
        # Hold back a trailing PARTIAL line instead of consuming it: wptserve
        # writes from several processes, so the tail of the file is routinely a
        # half-written line.  Consuming it would mis-parse it now and lose its
        # remainder on the next read.
        if not data.endswith("\n"):
            cut = data.rfind("\n") + 1
            end -= len(data) - cut
            data = data[:cut]
        self.pos = end

        now = time.time()
        for line in data.splitlines():
            if WS_HANDSHAKE_RE.match(line):
                self.ws_handshakes += 1
                continue
            rw = REWRITE_RE.match(line)
            if rw:
                self._aliases[rw.group("dst")] = rw.group("src")
                continue
            m = RESP_RE.match(line)
            if m:
                path = self._aliases.get(m.group("path"), m.group("path"))
                ref = clean_referer(m.group("referer"))
                self._answer((int(m.group("port")), m.group("method"), path))
                self._file({"port": int(m.group("port")),
                            "method": m.group("method"),
                            "path": path,
                            "referer": ref,
                            "status": int(m.group("status")),
                            "length": int(m.group("length")),
                            "t": now})
                continue
            q = REQ_RE.match(line)
            if q:
                path = self._aliases.get(q.group("path"), q.group("path"))
                rec = {"port": int(q.group("port")), "method": q.group("method"),
                       "path": path, "referer": "", "status": None,
                       "length": 0, "t": now, "unanswered": True}
                with self._lock:
                    self._pending.setdefault(
                        (rec["port"], rec["method"], path), []).append(rec)

    def _answer(self, key: tuple) -> None:
        """Retire the oldest pending request line this response answers."""
        with self._lock:
            q = self._pending.get(key)
            if q:
                q.pop(0)
                if not q:
                    del self._pending[key]

    def _file(self, rec: dict) -> None:
        rp = url_path(rec["referer"])
        with self._lock:
            if not rp:
                # No referer.  If it is the navigation TO an open test, that
                # test owns it (and it is noise).  Otherwise -- a
                # browser-process fetch, say -- nothing in the line names the
                # test that caused it.
                nav = [b for own, b in self._bins.values()
                       if rec["path"] in own.exact
                       or strip_query(rec["path"]) in own.exact]
                if len(nav) == 1:
                    nav[0].append(rec)
                else:
                    self._orphan(rec)
                return
            # Exact document first -- unambiguous whatever else is running.
            if rp in self._bins:
                self._bins[rp][1].append(rec)
                return
            # Then the test's wrapper scripts (requests made inside its
            # workers).  More than one claimant means the schedule let two
            # siblings overlap; that line is not guessed at.
            claim = [b for own, b in self._bins.values() if rp in own]
            if len(claim) == 1:
                claim[0].append(rec)
            else:
                # A nested frame's own subresource, or an ambiguous claim.
                self._orphan(rec)

    def _orphan(self, rec: dict) -> None:       # caller holds the lock
        self._orphans.append(rec)
        self.orphan_total += 1


def dedup(records: list) -> list:
    """Collapse wptserve's request+response pair into one record per request.

    Keyed by (port, method, path): the answered line wins, because it carries
    the status, and an unanswered line survives only when nothing answered it.
    """
    out: dict = {}
    for r in records:
        k = (r["port"], r["method"], r["path"])
        if k not in out or out[k].get("unanswered"):
            out[k] = r
    return list(out.values())
