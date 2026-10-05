"""The test list, read from WPT's own MANIFEST.json."""
from __future__ import annotations

import json
import os
import posixpath
from dataclasses import dataclass, field

from .config import EXCLUDE_PREFIXES, HTTP_ORIGIN, HTTPS_ORIGIN


@dataclass(frozen=True)
class Test:
    url: str          # e.g. /fetch/api/basic/request-head.any.html
    type: str         # testharness | reftest | ...
    # Helper scripts the test DECLARES (`// META: script=...`), as absolute
    # paths.  Loading them is part of loading the test, like testharness.js.
    deps: tuple = field(default=(), compare=False)

    @property
    def origin(self) -> str:
        """https for the .https / .serviceworker / .h2 naming convention."""
        u = self.url
        secure = ".https." in u or ".serviceworker." in u or ".h2." in u
        return HTTPS_ORIGIN if secure else HTTP_ORIGIN

    @property
    def full_url(self) -> str:
        return self.origin + self.url

    @property
    def path(self) -> str:
        return self.url.split("?", 1)[0].split("#", 1)[0]


def load(wpt: str) -> dict:
    path = os.path.join(wpt, "MANIFEST.json")
    if not os.path.exists(path):
        raise SystemExit(f"no MANIFEST.json in {wpt} -- run `./wpt manifest` there")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _deps(url: str, extras) -> tuple:
    """Absolute paths of the `META: script=` dependencies in a manifest entry."""
    if not isinstance(extras, dict):
        return ()
    base = posixpath.dirname(url)
    out = []
    for item in extras.get("script_metadata") or []:
        if len(item) == 2 and item[0] == "script":
            dep = item[1].split("?", 1)[0]
            out.append(dep if dep.startswith("/")
                       else posixpath.normpath(posixpath.join(base, dep)))
    return tuple(out)


def tests(manifest: dict, types: tuple, prefix: str = "") -> list[Test]:
    """Every runnable test URL, in tree order, de-duplicated.

    MANIFEST leaves are [source_hash, [url, extras], ...].  A url of None means
    the URL is the source path, and one source can expand into several URLs
    (the ?include= variants and .any.js / .window.js / .worker.js expansions).
    """
    # comma-separated: "webrtc/,webtransport/" selects either
    prefixes = tuple(p.strip().strip("/") for p in prefix.split(",") if p.strip())
    out: list[Test] = []
    seen: set = set()

    def walk(node, path: str, kind: str) -> None:
        if isinstance(node, dict):
            for k in sorted(node):
                walk(node[k], f"{path}/{k}" if path else k, kind)
            return
        if not isinstance(node, list):
            return
        for entry in node[1:]:
            if not isinstance(entry, list):
                continue
            url = entry[0] if entry and isinstance(entry[0], str) else path
            url = url if url.startswith("/") else "/" + url
            rel = url.lstrip("/")
            if prefixes and not rel.startswith(prefixes):
                continue
            if rel.startswith(EXCLUDE_PREFIXES):
                continue
            if url not in seen:
                seen.add(url)
                extras = entry[-1] if len(entry) > 1 else None
                out.append(Test(url, kind, _deps(url, extras)))

    for kind in types:
        walk(manifest.get("items", {}).get(kind, {}), "", kind)
    return out


def source_files(manifest: dict, urls: set) -> dict[str, str]:
    """Map each selected test URL back to the SOURCE FILE that produces it.

    One source (foo.any.js) becomes several URLs (foo.any.html,
    foo.any.worker.html); copying a subtree needs the source, not the URL.
    """
    found: dict[str, str] = {}

    def walk(node, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}/{k}" if path else k)
            return
        if not isinstance(node, list):
            return
        for entry in node[1:]:
            if not isinstance(entry, list):
                continue
            url = entry[0] if entry and isinstance(entry[0], str) else path
            url = url if url.startswith("/") else "/" + url
            if url in urls:
                found[url] = path

    for kind, tree in manifest.get("items", {}).items():
        walk(tree, "")
    return found
