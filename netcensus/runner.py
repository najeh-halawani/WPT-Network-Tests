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
from .classify import TestResult, classify
from .config import RunConfig
from .console import Console
from .server import WptServe

# A browser is replaced after this many tests.  Long-lived headless Chromes
# accumulate service workers, storage and stray targets, and one of those
# leaking into the next test's tab is a wrong row nobody would notice.
RECYCLE_EVERY = 200


@dataclass
class Census:
    cfg: RunConfig
    out: str
    console: Console
    resume: bool = False

    @property
    def checkpoint(self) -> str:
        return os.path.splitext(self.out)[0] + ".jsonl"

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
        if not self.resume and os.path.exists(self.checkpoint):
            os.remove(self.checkpoint)

        c.header(f"netcensus: {len(todo)} test(s) to run"
                 + (f", {len(done)} already decided" if done else "")
                 + f"  ·  {self.cfg.jobs} worker(s)")
        c.info(f"wpt     {self.cfg.wpt}")
        c.info(f"chrome  {self.cfg.chrome}")

        log_path = proc.tmpdir("netcensus_wptserve_access.log")
        c.info(f"starting wpt serve --verbose  (access log: {log_path})")
        orphan_total = 0
        with WptServe(self.cfg.wpt, log_path):
            router = accesslog.LogRouter(log_path)
            router.start()
            try:
                self._drive(todo, router)
            finally:
                router.orphans(flush_pending=True)
                orphan_total = router.orphan_total
                router.stop()
        summary = self.finalize(orphan_requests=orphan_total)
        c.summary(summary)
        c.info(f"results  {self.out}")
        c.info(f"list     {os.path.splitext(self.out)[0]}.txt")
        return summary

    @staticmethod
    def _groups(tests: list) -> list[list]:
        """Tests that share wrapper scripts, in tree order, one list each."""
        groups: dict = {}
        for t in tests:
            groups.setdefault(accesslog.OwnDocs(t.url).group, []).append(t)
        return list(groups.values())

    def _drive(self, todo: list, router: accesslog.LogRouter) -> None:
        work = collections.deque(self._groups(todo))
        lock = threading.Lock()
        sink = open(self.checkpoint, "a", encoding="utf-8", newline="\n")
        total, counter = len(todo), [0]

        def next_group():
            with lock:
                return work.popleft() if work else None

        def record(result: TestResult) -> None:
            with lock:
                sink.write(json.dumps(result.to_dict()) + "\n")
                sink.flush()
                counter[0] += 1
                idx = counter[0]
            self.console.test_result(idx, total, result)

        def worker(slot: int) -> None:
            browser, used = None, 0
            try:
                while (group := next_group()) is not None:
                    for test in group:
                        if browser is None or used >= RECYCLE_EVERY \
                                or not browser.cdp.alive:
                            if browser:
                                browser.close()
                            browser, used = Browser(self.cfg.chrome, slot), 0
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
    def finalize(self, orphan_requests: int = 0) -> dict:
        rows: list[dict] = []
        if os.path.exists(self.checkpoint):
            with open(self.checkpoint, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        # last row for a URL wins, so a re-verified test replaces its old row
        by_url = {r["url"]: r for r in rows}
        rows = sorted(by_url.values(), key=lambda r: r["url"])
        emitted = [r for r in rows if r["emitted"]]
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
            "n_emitted": len(emitted),
            "n_cdp_only": sum(1 for r in rows if r.get("cdp_only")),
            "n_incomplete": sum(1 for r in rows if not r.get("completed")),
            "n_errors": sum(1 for r in rows if r.get("error")),
            "orphan_requests": orphan_requests,
            "rows": rows,
        }
        with open(self.out, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(summary, fh, indent=1)
        with open(os.path.splitext(self.out)[0] + ".txt", "w",
                  encoding="utf-8", newline="\n") as fh:
            fh.writelines(r["url"] + "\n" for r in emitted)
        return summary
