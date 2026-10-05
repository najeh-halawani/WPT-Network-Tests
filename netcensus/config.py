"""Every path, port and default the kit depends on, stated once."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT = os.path.abspath(os.path.join(KIT, "..", ".."))

SERVE_CONFIG = os.path.join(KIT, "wpt_serve_config.json")
DATA_DIR = os.path.join(KIT, "data")

# Ports wptserve binds with wpt_serve_config.json.  All of them must be free
# before a run: one held by a dead previous server takes the whole server down.
REQUIRED_PORTS = (8000, 8001, 8002, 8010, 8443, 8444, 8445, 8446,
                  8888, 8889, 9000)
HTTP_ORIGIN = "http://web-platform.test:8000"
HTTPS_ORIGIN = "https://web-platform.test:8443"

# Test types worth running.  testharness is the bulk of the tree; reftests,
# print-reftests and crashtests still load subresources, so they can emit.
# `manual` needs a human and `support` is not a test.
RUNNABLE_TYPES = ("testharness", "reftest", "print-reftest", "crashtest")

# Never part of a census, whatever the manifest says.  `lna-retarget/` holds
# the LNA harness's MODIFIED copies of WPT tests (generated into the WPT
# checkout by ../dynamic-harness); this kit measures stock tests only.
EXCLUDE_PREFIXES = ("lna-retarget/",)


def _default_wpt() -> str:
    return os.environ.get("WPT_ROOT", os.path.join(PROJECT, "wpt"))


def _default_chrome() -> str:
    if os.environ.get("WPT_CHROME"):
        return os.environ["WPT_CHROME"]
    win = os.path.join(PROJECT, "browsers", "chrome-win64", "chrome.exe")
    if os.path.exists(win):
        return win
    for p in ("/usr/bin/google-chrome", "/usr/bin/chromium",
              "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
        if os.path.exists(p):
            return p
    return win


@dataclass
class RunConfig:
    wpt: str = field(default_factory=_default_wpt)
    chrome: str = field(default_factory=_default_chrome)
    jobs: int = 8
    timeout: float = 20.0      # wait this long for testharness to report
    settle: float = 1.5        # then keep watching this long for late requests
    types: tuple = RUNNABLE_TYPES
    filter: str = ""
    limit: int = 0
    require_all_servers: bool = True   # refuse to run with a protocol server down
    # After the parallel pass, re-run serially the tests under these prefixes
    # that showed no runtime evidence (WebRTC is timing-bound under load).
    serial_recheck: str = "webrtc"
