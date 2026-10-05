"""Run a set of tests and decide, per test, whether it emitted network traffic.

ATTRIBUTION, AND WHY THE RUN CAN BE PARALLEL
A logged request is credited to the test its Referer names.  That needs no time
window, so N tests in flight cannot contaminate each other's rows.  What
parallelism does cost: a request with no referer (a browser-process fetch), or
one issued by a nested document (an iframe's own subresource), cannot be tied
to a single test.  Those are counted run-level as `orphan_requests` and never
charged to anyone -- so a parallel run can UNDERSTATE a test's traffic but can
never invent any.  `verify` runs one test at a time and shows them all.

SCHEDULING
The unit of work is a SOURCE FILE, not a URL.  foo.any.js is served as
foo.any.html, foo.any.worker.html, foo.any.sharedworker.html, ... and the
worker variants all load foo.any.worker.js -- so requests made inside one of
those workers carry a Referer that every sibling could claim.  Running a
source's variants back to back on one worker means no two claimants are ever
in flight, which keeps that attribution exact rather than a guess.

CHECKPOINTING
Each row is appended to <out>.jsonl the moment it is decided.  A whole-tree run
takes hours; a crash after four of them must not cost four hours.  --resume
skips every URL already in the checkpoint, and the final JSON is always rebuilt
from the checkpoint, so a resumed run and a clean one produce the same file.
"""
from __future__ import annotations

import collections
import json
import os
import threading
import time
from dataclasses import dataclass

from . import accesslog, manifest, proc
from .browser import Browser, Visit
from .classify import PROTOCOLS, TestResult, classify
from .config import RunConfig
from .console import Console
from .server import WptServe

# A browser is replaced after this many tests.  Long-lived headless Chromes
# accumulate service workers, storage and stray targets, and one of those
# leaking into the next test's tab is a wrong row nobody would notice.
RECYCLE_EVERY = 200
# A progress line (done/total, rate, ETA, running tallies) every this many.
PROGRESS_EVERY = 250


@dataclass
class Census:
    cfg: RunConfig
    out: str
    console: Console
    resume: bool = False

    @property
    def checkpoint(self) -> str:
        return os.path.splitext(self.out)[0] + ".jsonl"

    @property
    def ledger(self) -> str:
        """One line per run session: the run-level counts that belong to no
        row (orphans, server-side WebSocket handshakes).  Kept apart from the
        rows so --resume adds a session instead of forgetting the last one."""
        return os.path.splitext(self.out)[0] + ".sessions.jsonl"

    def _log_session(self, session: dict) -> None:
        with open(self.ledger, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(session) + "\n")

    def _sessions(self) -> list[dict]:
        if not os.path.exists(self.ledger):
            return []
        out = []
        with open(self.ledger, encoding="utf-8") as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        return out

    # -- selection ------------------------------------------------------------
    def select(self) -> list[manifest.Test]:
        man = manifest.load(self.cfg.wpt)
        tests = manifest.tests(man, self.cfg.types, self.cfg.filter)
        return tests[:self.cfg.limit] if self.cfg.limit else tests

    def _already_done(self) -> set:
        if not (self.resume and os.path.exists(self.checkpoint)):
            return set()
        done = set()
        with open(self.checkpoint, encoding="utf-8") as fh:
            for line in fh:
                try:
                    done.add(json.loads(line)["url"])
                except (ValueError, KeyError):
                    continue          # a torn final line from a killed run
        return done

    # -- run ------------------------------------------------------------------
    def run(self) -> dict:
        c = self.console
        os.makedirs(os.path.dirname(os.path.abspath(self.out)), exist_ok=True)
        tests = self.select()
        done = self._already_done()
        todo = [t for t in tests if t.url not in done]
        if not self.resume:
            for stale in (self.checkpoint, self.ledger):
                if os.path.exists(stale):
                    os.remove(stale)

        c.header(f"netcensus: {len(todo)} test(s) to run"
                 + (f", {len(done)} already decided" if done else "")
                 + f"  ·  {self.cfg.jobs} worker(s)")
        c.info(f"wpt     {self.cfg.wpt}")
        c.info(f"chrome  {self.cfg.chrome}")

        log_path = proc.tmpdir("netcensus_wptserve_access.log")
        c.info(f"starting wpt serve --verbose  (access log: {log_path})")
        session = {"started": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "rows": 0, "ws_attributed": 0}
        with WptServe(self.cfg.wpt, log_path,
                      self.cfg.require_all_servers) as serve:
            c.servers(serve.servers)
            session["servers"] = serve.servers
            router = accesslog.LogRouter(log_path)
            router.start()
            try:
                self._drive(todo, router, session)
            finally:
                router.orphans(flush_pending=True)
                router.stop()
                session.update(ended=time.strftime("%Y-%m-%dT%H:%M:%S"),
                               orphans=router.orphan_total,
                               ws_server=router.ws_handshakes)
                self._log_session(session)
        summary = self.finalize()
        c.summary(summary)
        c.info(f"results  {self.out}")
        c.info(f"runtime  {os.path.splitext(self.out)[0]}.txt")
        c.info(f"static   {os.path.splitext(self.out)[0]}-static.txt")
        return summary

    @staticmethod
    def _groups(tests: list) -> list[list]:
        """Tests that share wrapper scripts, in tree order, one list each."""
        groups: dict = {}
        for t in tests:
            groups.setdefault(accesslog.OwnDocs(t.url).group, []).append(t)
        return list(groups.values())

    def _drive(self, todo: list, router: accesslog.LogRouter,
               session: dict) -> None:
        work = collections.deque(self._groups(todo))
        lock = threading.Lock()
        sink = open(self.checkpoint, "a", encoding="utf-8", newline="\n")
        total = len(todo)
        t0 = time.time()
        tally = {"runtime": 0, "static": 0, "error": 0}

        def next_group():
            with lock:
                return work.popleft() if work else None

        def record(result: TestResult) -> None:
            with lock:
                sink.write(json.dumps(result.to_dict()) + "\n")
                sink.flush()
                session["rows"] += 1
                session["ws_attributed"] += result.n_ws
                idx = session["rows"]
                if result.tier in tally:
                    tally[result.tier] += 1
                snap = dict(tally)
            self.console.test_result(idx, total, result)
            if idx % PROGRESS_EVERY == 0 or idx == total:
                self.console.progress(idx, total, time.time() - t0, snap)

        def worker(slot: int) -> None:
            browser, used = None, 0
            try:
                while (group := next_group()) is not None:
                    for test in group:
                        if browser is None or used >= RECYCLE_EVERY \
                                or not browser.cdp.alive:
                            if browser:
                                browser.close()
                            browser, used = Browser(self.cfg.chrome, slot, self.cfg.wpt), 0
                        used += 1
                        record(self._one(browser, test, router))
            except Exception as exc:                       # pragma: no cover
                self.console.error(f"worker {slot} died: {exc}")
            finally:
                if browser:
                    browser.close()

        threads = [threading.Thread(target=worker, args=(i,), daemon=True)
                   for i in range(max(1, self.cfg.jobs))]
        try:
            for t in threads:
                t.start()
            while any(t.is_alive() for t in threads):
                for t in threads:
                    t.join(timeout=0.5)
        except KeyboardInterrupt:
            self.console.warn("interrupted -- finished rows are kept; "
                              "re-run with --resume to continue")
            with lock:
                work.clear()
            for t in threads:
                t.join(timeout=self.cfg.timeout + self.cfg.settle + 5)
        finally:
            sink.close()

    def _one(self, browser: Browser, test: manifest.Test,
             router: accesslog.LogRouter) -> TestResult:
        key = router.open_test(test.url)
        try:
            visit = browser.visit(test.full_url, self.cfg.timeout, self.cfg.settle)
        except Exception as exc:
            visit = Visit(error=f"{type(exc).__name__}: {exc}"[:300])
        return classify(test, visit, router.take(key))

    # -- output ---------------------------------------------------------------
    def finalize(self) -> dict:
        rows: list[dict] = []
        if os.path.exists(self.checkpoint):
            with open(self.checkpoint, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        decided = len(rows)
        sessions = self._sessions()
        ws_server = sum(s.get("ws_server", 0) for s in sessions)
        ws_attributed = sum(s.get("ws_attributed", 0) for s in sessions)
        # last row for a URL wins, so a re-verified test replaces its old row
        by_url = {r["url"]: r for r in rows}
        rows = sorted(by_url.values(), key=lambda r: r["url"])
        runtime = [r for r in rows if r.get("runtime")]
        static_only = [r for r in rows if r["emitted"] and not r.get("runtime")]
        summary = {
            "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "wpt": self.cfg.wpt,
            "chrome": self.cfg.chrome,
            "filter": self.cfg.filter,
            "types": list(self.cfg.types),
            "jobs": self.cfg.jobs,
            "timeout": self.cfg.timeout,
            "settle": self.cfg.settle,
            "n_tests": len(rows),
            "n_emitted": len(runtime) + len(static_only),
            "n_runtime": len(runtime),
            "n_static_only": len(static_only),
            "n_none": sum(1 for r in rows if not r["emitted"]
                          and not r.get("cdp_only") and not r.get("error")),
            "n_cdp_only": sum(1 for r in rows if r.get("cdp_only")),
            "n_incomplete": sum(1 for r in rows if not r.get("completed")),
            "n_errors": sum(1 for r in rows if r.get("error")),
            # -- run-level, summed over every session (see `ledger`) ----------
            "sessions": len(sessions),
            # Rows decided by a session that was killed before it could log
            # its totals.  Non-zero means the counts below are a lower bound.
            "rows_without_session": max(0, decided - sum(s.get("rows", 0)
                                                         for s in sessions)),
            "orphan_requests": sum(s.get("orphans", 0) for s in sessions),
            # Cross-check of the WebSocket evidence: handshakes the ws/wss
            # SERVER logged vs accepted handshakes credited to tests.  Equal
            # means every one is accounted for; server > attributed means
            # some came from frames/targets the census did not see.
            "ws_handshakes_server": ws_server,
            "ws_handshakes_attributed": ws_attributed,
            # protocol servers that were DOWN in any session (only possible
            # with --allow-missing-servers).  Tests on those protocols are
            # unmeasured, not negative.
            "servers_down": sorted({k for s in sessions
                                    for k, ok in s.get("servers", {}).items()
                                    if not ok}),
            # tests with runtime evidence on each protocol (a test can be in
            # several: a WebRTC test that also fetches is http + webrtc)
            "n_by_protocol": {p: sum(1 for r in runtime
                                     if p in r.get("protocols", []))
                              for p in PROTOCOLS},
            "rows": rows,
        }
        with open(self.out, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(summary, fh, indent=1)
        stem = os.path.splitext(self.out)[0]
        # <out>.txt             tests that emit at RUN time  (the headline list)
        # <out>-static.txt      tests whose only traffic is markup / META deps
        # <out>-<protocol>.txt  the runtime list, split by protocol
        lists = [(stem + ".txt", runtime), (stem + "-static.txt", static_only)]
        lists += [(f"{stem}-{p}.txt",
                   [r for r in runtime if p in r.get("protocols", [])])
                  for p in PROTOCOLS]
        for path, sel in lists:
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.writelines(r["url"] + "\n" for r in sel)
        return summary
