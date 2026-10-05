"""Unit tests for everything that decides the answer.  No browser, no server.

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from netcensus import accesslog, manifest                    # noqa: E402
from netcensus.browser import Visit                          # noqa: E402
from netcensus.classify import TestResult, classify          # noqa: E402
from netcensus.console import Console                        # noqa: E402

TEST = "/fetch/api/basic/request-head.any.html"
REF = f"http://web-platform.test:8000{TEST}"


def resp(path, referer=REF, status=200, port=8000, method="GET", length=10):
    return (f"[2026-10-05 12:00:00,000 http on port {port}] DEBUG - "
            f"{status} {method} {path} ({referer}) {length}\n")


def req(path, port=8000, method="GET"):
    return f"[2026-10-05 12:00:00,000 http on port {port}] DEBUG - {method} {path}\n"


class LogFixture(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".log")
        os.close(fd)
        self.router = accesslog.LogRouter(self.path, poll=3600)  # manual drain
        self.router.start()

    def tearDown(self):
        self.router.stop()
        os.remove(self.path)

    def write(self, text):
        with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(text)


class TestParsing(LogFixture):
    def test_response_line_attributed_by_referer(self):
        self.router.open_test(TEST)
        self.write(resp("/fetch/api/resources/top.txt"))
        got = self.router.take(TEST)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["path"], "/fetch/api/resources/top.txt")
        self.assertEqual(got[0]["status"], 200)
        self.assertEqual(got[0]["port"], 8000)

    def test_referer_query_is_ignored_for_attribution(self):
        self.router.open_test(TEST)
        self.write(resp("/x.txt", referer=REF + "?variant=1#frag"))
        self.assertEqual(len(self.router.take(TEST)), 1)

    def test_other_tests_lines_are_not_ours(self):
        self.router.open_test(TEST)
        self.write(resp("/x.txt", referer="http://web-platform.test:8000/other.html"))
        self.assertEqual(self.router.take(TEST), [])
        self.assertEqual(len(self.router.orphans()), 1)

    def test_no_referer_is_orphaned_not_charged(self):
        self.router.open_test(TEST)
        self.write(resp("/browser-process-fetch", referer="None"))
        self.assertEqual(self.router.take(TEST), [])
        self.assertEqual(len(self.router.orphans()), 1)

    def test_partial_trailing_line_is_held_back(self):
        self.router.open_test(TEST)
        line = resp("/late.txt")
        self.write(line[:30])
        self.assertEqual(self.router.take(TEST), [])      # not yet complete
        self.router.open_test(TEST)
        self.write(line[30:])
        self.assertEqual([r["path"] for r in self.router.take(TEST)], ["/late.txt"])

    def test_rewrite_alias_is_undone(self):
        self.router.open_test(TEST)
        self.write("[2026-10-05 12:00:00,000 http on port 8000] DEBUG - Rewriting "
                   "request path /resources/WebIDLParser.js to "
                   "/resources/webidl2/lib/webidl2.js\n")
        self.write(resp("/resources/webidl2/lib/webidl2.js"))
        got = self.router.take(TEST)
        self.assertEqual(got[0]["path"], "/resources/WebIDLParser.js")

    def test_request_line_is_paired_with_its_response(self):
        key = self.router.open_test(TEST)
        self.write(req("/fetch/api/resources/top.txt"))
        self.write(resp("/fetch/api/resources/top.txt"))
        got = self.router.take(key)
        self.assertEqual([(r["path"], r["status"]) for r in got],
                         [("/fetch/api/resources/top.txt", 200)])
        self.assertEqual(self.router.orphans(flush_pending=True), [])

    def test_unanswered_request_surfaces_as_orphan(self):
        self.router.open_test(TEST)
        self.write(req("/hangs-forever"))
        self.assertEqual(self.router.orphans(), [])           # still young
        got = self.router.orphans(flush_pending=True)
        self.assertEqual([r["path"] for r in got], ["/hangs-forever"])
        self.assertTrue(got[0]["unanswered"])
        self.assertEqual(self.router.orphan_total, 1)

    def test_navigation_without_referer_belongs_to_its_test(self):
        key = self.router.open_test(TEST)
        self.write(resp(TEST, referer=None))
        self.assertEqual([r["path"] for r in self.router.take(key)], [TEST])
        self.assertEqual(self.router.orphan_total, 0)

    def test_lines_before_start_are_ignored(self):
        self.router.stop()
        self.write(resp("/before.txt"))
        r = accesslog.LogRouter(self.path, poll=3600)
        r.start()
        r.open_test(TEST)
        self.assertEqual(r.take(TEST), [])
        r.stop()


class TestWrapperAttribution(LogFixture):
    ANY = "/fetch/api/basic/request-head.any.html"
    WORKER = "/fetch/api/basic/request-head.any.worker.html"
    WORKER_JS = "http://web-platform.test:8000/fetch/api/basic/request-head.any.worker.js"

    def test_request_from_inside_worker_is_attributed(self):
        key = self.router.open_test(self.WORKER)
        self.write(resp("/fetch/api/resources/top.txt", referer=self.WORKER_JS))
        self.assertEqual([r["path"] for r in self.router.take(key)],
                         ["/fetch/api/resources/top.txt"])

    def test_two_siblings_in_flight_is_ambiguous_not_guessed(self):
        k1 = self.router.open_test(self.WORKER)
        k2 = self.router.open_test(
            "/fetch/api/basic/request-head.any.sharedworker.html")
        self.write(resp("/x.txt", referer=self.WORKER_JS))
        self.assertEqual(self.router.take(k1), [])
        self.assertEqual(self.router.take(k2), [])
        self.assertEqual(len(self.router.orphans()), 1)

    def test_exact_document_wins_over_sibling_stem(self):
        k_any = self.router.open_test(self.ANY)
        k_wrk = self.router.open_test(self.WORKER)
        self.write(resp("/a.txt", referer="http://web-platform.test:8000" + self.ANY))
        self.assertEqual(len(self.router.take(k_any)), 1)
        self.assertEqual(self.router.take(k_wrk), [])


class TestOwnDocs(unittest.TestCase):
    def test_generated_wrappers_are_the_test(self):
        own = accesslog.OwnDocs("/fetch/api/basic/request-head.any.worker.html")
        for p in ("/fetch/api/basic/request-head.any.worker.html",
                  "/fetch/api/basic/request-head.any.js",
                  "/fetch/api/basic/request-head.any.worker.js",
                  "/fetch/api/basic/request-head.any.worker.js?x=1"):
            self.assertIn(p, own, p)

    def test_unrelated_files_with_similar_names_are_not(self):
        own = accesslog.OwnDocs("/fetch/api/basic/request-head.any.html")
        for p in ("/fetch/api/basic/request-head.txt",
                  "/fetch/api/basic/request-head-other.any.js",
                  "/fetch/api/basic/resources/request-head.any.js",
                  "/fetch/api/basic/request-head.any.json"):
            self.assertNotIn(p, own, p)

    def test_plain_test_has_no_stem(self):
        own = accesslog.OwnDocs("/dom/nodes/Node-appendChild.html?x")
        self.assertIsNone(own.stem)
        self.assertIn("/dom/nodes/Node-appendChild.html", own)
        self.assertNotIn("/dom/nodes/Node-appendChild.js", own)

    def test_variants_share_a_group(self):
        g = {accesslog.OwnDocs(u).group for u in (
            "/a/foo.any.html", "/a/foo.any.worker.html",
            "/a/foo.any.serviceworker.html", "/a/foo.https.any.html")}
        self.assertEqual(len(g), 2)          # foo.* and foo.https.*

    def test_clean_referer(self):
        self.assertEqual(accesslog.clean_referer("b'http://h/a'"), "http://h/a")
        self.assertEqual(accesslog.clean_referer('b"http://h/a"'), "http://h/a")
        self.assertEqual(accesslog.clean_referer("None"), "")
        self.assertEqual(accesslog.clean_referer("http://h/a"), "http://h/a")


class TestScheduling(unittest.TestCase):
    def test_variants_of_one_source_form_one_unit(self):
        from netcensus.runner import Census
        T = manifest.Test
        groups = Census._groups([T("/a/foo.any.html", "t"),
                                 T("/a/foo.any.worker.html", "t"),
                                 T("/a/bar.html", "t"),
                                 T("/a/foo.any.serviceworker.html", "t")])
        self.assertEqual([[t.url for t in g] for g in groups],
                         [["/a/foo.any.html", "/a/foo.any.worker.html",
                           "/a/foo.any.serviceworker.html"], ["/a/bar.html"]])


class TestDedupAndNoise(unittest.TestCase):
    def test_request_and_response_lines_collapse(self):
        recs = [{"port": 8000, "method": "GET", "path": "/a", "status": None,
                 "unanswered": True},
                {"port": 8000, "method": "GET", "path": "/a", "status": 200}]
        out = accesslog.dedup(recs)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["status"], 200)

    def test_unanswered_request_still_counts(self):
        recs = [{"port": 8000, "method": "GET", "path": "/hang", "status": None,
                 "unanswered": True}]
        self.assertEqual(len(accesslog.dedup(recs)), 1)

    def test_harness_boilerplate_is_noise(self):
        sp = accesslog.self_paths(TEST)
        for p in ("/resources/testharness.js", "/resources/testharnessreport.js",
                  "/resources/testdriver.js", "/resources/testdriver-vendor.js",
                  "/favicon.ico", TEST, TEST + "?x=1",
                  "/fetch/api/basic/request-head.any.js"):
            self.assertTrue(accesslog.is_noise(p, sp), p)

    def test_real_subresources_are_not_noise(self):
        sp = accesslog.self_paths(TEST)
        for p in ("/fetch/api/resources/top.txt", "/common/get-host-info.sub.js",
                  "/resources/blank.html", "/images/green.png",
                  "/interfaces/fetch.idl"):
            self.assertFalse(accesslog.is_noise(p, sp), p)

    def test_url_path(self):
        self.assertEqual(accesslog.url_path("https://h:1/a/b?c#d"), "/a/b")
        self.assertEqual(accesslog.url_path("https://h:1"), "/")
        self.assertEqual(accesslog.url_path(""), "")
        self.assertEqual(accesslog.url_path("None"), "")


class TestClassify(unittest.TestCase):
    T = manifest.Test(TEST, "testharness")

    def rec(self, path, status=200):
        return {"port": 8000, "method": "GET", "path": path, "status": status,
                "referer": REF}

    def test_only_boilerplate_is_not_emitting(self):
        r = classify(self.T, Visit(completed=True),
                     [self.rec(TEST), self.rec("/resources/testharness.js")])
        self.assertFalse(r.emitted)
        self.assertEqual(r.n_logged, 0)

    def test_own_request_is_emitting(self):
        r = classify(self.T, Visit(completed=True),
                     [self.rec(TEST), self.rec("/fetch/api/resources/top.txt", 404)])
        self.assertTrue(r.emitted)
        self.assertEqual(r.logged, ["GET :8000/fetch/api/resources/top.txt"])
        self.assertEqual(r.statuses, [404])     # a 404 still hit the server

    def test_browser_attempt_without_server_hit_is_cdp_only(self):
        v = Visit(completed=True, requests=[
            {"url": "https://example.invalid/x", "method": "GET", "type": "Fetch"}])
        r = classify(self.T, v, [self.rec(TEST)])
        self.assertFalse(r.emitted)
        self.assertTrue(r.cdp_only)

    def test_non_network_schemes_ignored(self):
        v = Visit(requests=[{"url": "data:text/plain,x", "method": "GET", "type": ""},
                            {"url": "blob:http://h/1", "method": "GET", "type": ""}])
        r = classify(self.T, v, [])
        self.assertEqual(r.n_cdp, 0)
        self.assertFalse(r.cdp_only)

    def test_round_trip(self):
        r = classify(self.T, Visit(completed=True), [self.rec("/a")])
        self.assertEqual(TestResult.from_dict(json.loads(json.dumps(r.to_dict()))), r)


class TestManifest(unittest.TestCase):
    MAN = {"items": {
        "testharness": {"fetch": {"api": {
            "a.any.js": ["h", ["fetch/api/a.any.html", {}],
                         ["fetch/api/a.any.worker.html", {}]],
            "b.https.html": ["h", [None, {}]]}}},
        "reftest": {"css": {"r.html": ["h", [None, [["css/r-ref.html", "=="]], {}]]}},
        "support": {"fetch": {"api": {"resources": {"x.py": ["h", [None, {}]]}}}},
    }}

    def test_expansion_and_null_url(self):
        urls = [t.url for t in manifest.tests(self.MAN, ("testharness",))]
        self.assertEqual(urls, ["/fetch/api/a.any.html",
                                "/fetch/api/a.any.worker.html",
                                "/fetch/api/b.https.html"])

    def test_prefix_filter_and_types(self):
        got = manifest.tests(self.MAN, ("testharness", "reftest"), "css")
        self.assertEqual([(t.url, t.type) for t in got], [("/css/r.html", "reftest")])

    def test_lna_modified_copies_are_never_selected(self):
        m = {"items": {"testharness": {"lna-retarget": {
            "x.html": ["h", [None, {}]]}}}}
        self.assertEqual(manifest.tests(m, ("testharness",)), [])

    def test_origin_convention(self):
        self.assertTrue(manifest.Test("/a.https.html", "t").full_url.startswith("https://"))
        self.assertTrue(manifest.Test("/a.html", "t").full_url.startswith("http://"))
        self.assertTrue(manifest.Test("/a.serviceworker.html", "t").origin.startswith("https"))

    def test_source_files_maps_urls_back(self):
        m = manifest.source_files(self.MAN, {"/fetch/api/a.any.worker.html",
                                             "/fetch/api/b.https.html"})
        self.assertEqual(m, {"/fetch/api/a.any.worker.html": "fetch/api/a.any.js",
                             "/fetch/api/b.https.html": "fetch/api/b.https.html"})


class TestConsole(unittest.TestCase):
    def test_plain_output_has_no_escapes_and_two_lines(self):
        buf = io.StringIO()
        c = Console(color=False, stream=buf)
        r = TestResult(url=TEST, type="testharness", emitted=True, n_logged=1,
                       logged=["GET :8000/x"], completed=True)
        c.test_result(1, 10, r)
        out = buf.getvalue()
        self.assertNotIn("\033[", out)
        self.assertIn("testing", out)
        self.assertIn("network emits", out)
        self.assertEqual(out.count("\n"), 2)

    def test_quiet_hides_non_emitting(self):
        buf = io.StringIO()
        Console(color=False, stream=buf, quiet=True).test_result(
            1, 1, TestResult(url=TEST, type="testharness"))
        self.assertEqual(buf.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
