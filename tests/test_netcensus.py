"""Unit tests for everything that decides the answer.  No browser, no server.

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
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

    def test_websocket_server_handshakes_are_counted(self):
        self.write("[2026-10-05 12:58:37,535 ws on port 8888] DEBUG - "
                   "Protocol version is RFC 6455\n")
        self.write("[2026-10-05 12:58:37,535 wss on port 8889] DEBUG - "
                   "Protocol version is RFC 6455\n")
        self.write("[2026-10-05 12:58:37,535 ws on port 8888] DEBUG - Reset\n")
        self.router.orphans()                                  # forces a drain
        self.assertEqual(self.router.ws_handshakes, 2)

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


class TestTiers(unittest.TestCase):
    """runtime vs static: how each own request was initiated."""
    T = manifest.Test(TEST, "testharness", deps=("/fetch/api/resources/utils.js",))

    def rec(self, path, port=8000):
        return {"port": port, "method": "GET", "path": path, "status": 200,
                "referer": REF}

    def cdp(self, path, initiator, port=8000):
        return {"url": f"http://web-platform.test:{port}{path}", "method": "GET",
                "type": "", "initiator": initiator}

    def test_script_initiated_is_runtime(self):
        r = classify(self.T, Visit(requests=[self.cdp("/x.py?a=1", "script")]),
                     [self.rec("/x.py?a=1")])
        self.assertTrue(r.runtime)
        self.assertEqual(r.runtime_requests, ["GET :8000/x.py?a=1"])
        self.assertEqual(r.tier, "runtime")

    def test_parser_initiated_is_static(self):
        r = classify(self.T, Visit(requests=[self.cdp("/img.png", "parser")]),
                     [self.rec("/img.png")])
        self.assertTrue(r.emitted)
        self.assertFalse(r.runtime)
        self.assertEqual(r.tier, "static")

    def test_declared_meta_dependency_is_static_even_from_script(self):
        # in a worker variant importScripts() loads it: initiator is script
        r = classify(self.T, Visit(requests=[
            self.cdp("/fetch/api/resources/utils.js", "script")]),
            [self.rec("/fetch/api/resources/utils.js")])
        self.assertEqual(r.tier, "static")

    def test_same_url_from_markup_and_script_is_runtime(self):
        r = classify(self.T, Visit(requests=[self.cdp("/a.txt", "parser"),
                                             self.cdp("/a.txt", "script")]),
                     [self.rec("/a.txt")])
        self.assertTrue(r.runtime)

    def test_unseen_by_browser_is_runtime_not_static(self):
        # reached the server, CDP never saw it (browser-process fetch)
        r = classify(self.T, Visit(), [self.rec("/manifest.json")])
        self.assertTrue(r.runtime)

    def test_accepted_websocket_is_runtime_evidence(self):
        v = Visit(requests=[
            {"url": "ws://web-platform.test:8888/echo", "method": "GET",
             "type": "WebSocket", "initiator": "script", "status": 101},
            {"url": "wss://web-platform.test:8889/refused", "method": "GET",
             "type": "WebSocket", "initiator": "script", "status": None}])
        r = classify(self.T, v, [])
        self.assertTrue(r.emitted and r.runtime)
        self.assertEqual(r.runtime_requests, ["WS :8888/echo"])
        self.assertEqual(r.n_ws, 1)
        self.assertFalse(r.cdp_only)

    def test_websocket_sent_but_closed_while_connecting_counts(self):
        v = Visit(requests=[{"url": "ws://web-platform.test:8888/sleep", "method": "GET",
                             "type": "WebSocket", "initiator": "script",
                             "status": None, "sent": True}])
        r = classify(self.T, v, [])
        self.assertEqual((r.runtime, r.n_ws, r.protocols), (True, 1, ["websocket"]))

    def test_established_webtransport_is_evidence(self):
        v = Visit(requests=[
            {"url": "https://web-platform.test:54164/webtransport/handlers/echo.py",
             "method": "CONNECT", "type": "WebTransport", "initiator": "script",
             "established": True},
            {"url": "https://web-platform.test:54164/never", "method": "CONNECT",
             "type": "WebTransport", "initiator": "script", "established": False}])
        r = classify(self.T, v, [])
        self.assertEqual(r.runtime_requests, ["WT :54164/webtransport/handlers/echo.py"])
        self.assertEqual((r.n_wt, r.protocols), (1, ["webtransport"]))

    def test_failed_webtransport_is_cdp_only(self):
        v = Visit(requests=[{"url": "https://web-platform.test:1/x", "method": "CONNECT",
                             "type": "WebTransport", "initiator": "script",
                             "established": False}])
        self.assertEqual(classify(self.T, v, []).tier, "cdp-only")

    def test_ice_connected_peer_is_webrtc_evidence(self):
        pair = {"local": {"protocol": "udp", "type": "host", "address": "a.local", "port": 1},
                "remote": {"protocol": "udp", "type": "host", "address": "a.local", "port": 2}}
        r = classify(self.T, Visit(rtc=[{"state": "connected", "pair": pair},
                                        {"state": "connected", "pair": None}]), [])
        self.assertEqual(r.runtime_requests,
                         ["RTC ice-connected", "RTC udp host a.local:1 -> a.local:2"])
        self.assertEqual((r.n_rtc, r.protocols), (2, ["webrtc"]))

    def test_protocols_are_ordered_and_combined(self):
        v = Visit(requests=[{"url": "http://web-platform.test:8000/x", "method": "GET",
                             "type": "Fetch", "initiator": "script"}],
                  rtc=[{"state": "completed", "pair": None}])
        r = classify(self.T, v, [self.rec("/x")])
        self.assertEqual(r.protocols, ["http", "webrtc"])

    def test_refused_websocket_is_not_evidence(self):
        v = Visit(requests=[{"url": "ws://web-platform.test:8888/x", "method": "GET",
                             "type": "WebSocket", "initiator": "script",
                             "status": None}])
        r = classify(self.T, v, [])
        self.assertFalse(r.emitted)
        self.assertTrue(r.cdp_only)

    def test_https_default_port_join(self):
        v = Visit(requests=[{"url": "https://web-platform.test/p", "method": "GET",
                             "type": "", "initiator": "parser"}])
        r = classify(self.T, v, [self.rec("/p", port=443)])
        self.assertEqual(r.tier, "static")


class TestPreflight(unittest.TestCase):
    def test_server_ports_parsed_from_serve_log(self):
        from netcensus import server
        fd, p = tempfile.mkstemp(suffix=".log")
        os.close(fd)
        with open(p, "w") as fh:
            fh.write("noise\nDEBUG:root:Using ports: defaultdict(<class 'list'>, "
                     "{'http': [8000, 8010], 'ws': [8888], "
                     "'webtransport-h3': [54164], 'h2': [9000]})\n")
        self.assertEqual(server.server_ports(p)["webtransport-h3"], [54164])
        os.remove(p)

    def test_udp_port_held_detection(self):
        import socket
        from netcensus import server
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
        self.assertTrue(server._udp_held(port))
        sk.close()
        self.assertFalse(server._udp_held(port))


class TestSubtree(unittest.TestCase):
    def test_keeps_emitters_support_and_infra_drops_the_rest(self):
        from netcensus import subtree
        wpt = tempfile.mkdtemp()
        files = {
            "wpt": "", "tools/wpt/paths": "docs/\ntools/wpt/\n",
            "tools/wpt/commands.json": "{}", "docs/commands.json": "{}",
            "resources/testharness.js": "", "common/utils.js": "",
            "fetch/a.any.js": "", "fetch/b.html": "", "fetch/resources/x.py": "",
            "dom/c.html": "",
        }
        for rel, body in files.items():
            os.makedirs(os.path.join(wpt, os.path.dirname(rel)) or wpt, exist_ok=True)
            with open(os.path.join(wpt, rel), "w") as fh:
                fh.write(body)
        man = {"items": {"testharness": {
            "fetch": {"a.any.js": ["h", ["fetch/a.any.html", {}],
                                   ["fetch/a.any.worker.html", {}]],
                      "b.html": ["h", [None, {}]]},
            "dom": {"c.html": ["h", [None, {}]]}},
            "support": {"fetch": {"resources": {"x.py": ["h", [None, {}]]}}}}}
        with open(os.path.join(wpt, "MANIFEST.json"), "w") as fh:
            json.dump(man, fh)
        census = os.path.join(wpt, "c.json")
        with open(census, "w") as fh:
            json.dump({"rows": [
                {"url": "/fetch/a.any.worker.html", "emitted": True, "runtime": True,
                 "logged": ["GET :8000/fetch/resources/x.py"]},
                {"url": "/fetch/b.html", "emitted": False, "runtime": False},
                {"url": "/dom/c.html", "emitted": True, "runtime": False}]}, fh)
        out = os.path.join(tempfile.mkdtemp(), "tree")
        stats = subtree.build(wpt, census, out, Console(color=False, stream=io.StringIO()),
                              manifest=False)
        has = lambda rel: os.path.exists(os.path.join(out, rel))
        self.assertTrue(has("fetch/a.any.js"))          # source of the emitter
        self.assertTrue(has("fetch/resources/x.py"))    # its support file
        self.assertFalse(has("fetch/b.html"))           # did not emit
        self.assertFalse(has("dom/c.html"))             # static only (default)
        for infra in ("wpt", "docs/commands.json", "tools/wpt/paths",
                      "resources/testharness.js", "common/utils.js"):
            self.assertTrue(has(infra), infra)
        self.assertEqual(open(os.path.join(out, "NETWORK-TESTS.txt")).read().split(),
                         ["/fetch/a.any.worker.html"])
        self.assertEqual(stats["test_sources"], 1)


class TestFinalize(unittest.TestCase):
    """The end-of-run path: it runs once, hours in, so it is tested here."""

    def test_outputs_and_last_row_wins(self):
        from netcensus.config import RunConfig
        from netcensus.runner import Census
        d = tempfile.mkdtemp()
        out = os.path.join(d, "census.json")
        rows = [
            {"url": "/a.html", "type": "testharness", "emitted": True, "runtime": True},
            {"url": "/b.html", "type": "testharness", "emitted": True, "runtime": False},
            {"url": "/c.html", "type": "reftest", "emitted": False, "runtime": False},
            {"url": "/d.html", "type": "testharness", "emitted": False,
             "runtime": False, "cdp_only": True},
            {"url": "/c.html", "type": "reftest", "emitted": True, "runtime": True},
        ]
        with open(os.path.join(d, "census.jsonl"), "w") as fh:
            fh.writelines(json.dumps(r) + "\n" for r in rows)
        c = Census(RunConfig(wpt=d, chrome="x"), out, Console(color=False,
                                                              stream=io.StringIO()))
        # two sessions (a run and a --resume); a third was killed before
        # logging, which is what rows_without_session reports
        c._log_session({"rows": 2, "orphans": 5, "ws_server": 3, "ws_attributed": 3})
        c._log_session({"rows": 2, "orphans": 2, "ws_server": 1, "ws_attributed": 0})
        s = c.finalize()
        self.assertEqual((s["n_tests"], s["n_runtime"], s["n_static_only"],
                          s["n_none"], s["n_cdp_only"]), (4, 2, 1, 0, 1))
        self.assertEqual((s["sessions"], s["orphan_requests"],
                          s["ws_handshakes_server"], s["ws_handshakes_attributed"],
                          s["rows_without_session"]), (2, 7, 4, 3, 1))
        self.assertEqual(open(os.path.join(d, "census.txt")).read().split(),
                         ["/a.html", "/c.html"])
        self.assertEqual(open(os.path.join(d, "census-static.txt")).read().split(),
                         ["/b.html"])
        self.assertEqual(json.load(open(out))["n_tests"], 4)
        Console(color=False, stream=io.StringIO()).summary(s)   # renders


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

    def test_meta_script_deps_resolved(self):
        m = {"items": {"testharness": {"IndexedDB": {"x.any.js": [
            "h", ["IndexedDB/x.any.html", {"script_metadata": [
                ["global", "window"], ["script", "resources/support.js"],
                ["script", "/common/utils.js"], ["script", "../top.js?v=1"]]}]]}}}}
        (t,) = manifest.tests(m, ("testharness",))
        self.assertEqual(t.deps, ("/IndexedDB/resources/support.js",
                                  "/common/utils.js", "/top.js"))

    def test_source_files_maps_urls_back(self):
        m = manifest.source_files(self.MAN, {"/fetch/api/a.any.worker.html",
                                             "/fetch/api/b.https.html"})
        self.assertEqual(m, {"/fetch/api/a.any.worker.html": "fetch/api/a.any.js",
                             "/fetch/api/b.https.html": "fetch/api/b.https.html"})


class TestConsole(unittest.TestCase):
    def test_plain_output_has_no_escapes_and_two_lines(self):
        buf = io.StringIO()
        c = Console(color=False, stream=buf)
        r = TestResult(url=TEST, type="testharness", emitted=True, runtime=True,
                       n_logged=1, logged=["GET :8000/x"],
                       runtime_requests=["GET :8000/x"], completed=True)
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
