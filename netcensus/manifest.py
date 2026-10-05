"""The test list, read from WPT's own MANIFEST.json."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from .config import EXCLUDE_PREFIXES, HTTP_ORIGIN, HTTPS_ORIGIN


@dataclass(frozen=True)
class Test:
    url: str          # e.g. /fetch/api/basic/request-head.any.html
    type: str         # testharness | reftest | ...

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


def tests(manifest: dict, types: tuple, prefix: str = "") -> list[Test]:
    """Every runnable test URL, in tree order, de-duplicated.

    MANIFEST leaves are [source_hash, [url, extras], ...].  A url of None means
    the URL is the source path, and one source can expand into several URLs
    (the ?include= variants and .any.js / .window.js / .worker.js expansions).
    """
    prefix = prefix.strip("/")
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
            if prefix and not rel.startswith(prefix):
                continue
            if rel.startswith(EXCLUDE_PREFIXES):
                continue
            if url not in seen:
                seen.add(url)
                out.append(Test(url, kind))

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
