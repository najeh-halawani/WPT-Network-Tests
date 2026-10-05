"""Render the census as TREE.md: where the emitting tests live, and how the
MANIFEST.json that indexes them is shaped."""
from __future__ import annotations

import collections
import json
import os

MANIFEST_SHAPE = """\
```text
MANIFEST.json
├── version            manifest format version (wpt tooling checks it)
├── url_base           "/"
└── items
    ├── testharness    ← JS tests that report pass/fail (the bulk)
    │   └── fetch                      directory  →  nested object
    │       └── api
    │           └── basic
    │               └── request-head.any.js         source file  →  list
    │                   [ "<sha1 of source>",
    │                     ["fetch/api/basic/request-head.any.html",        {…}],
    │                     ["fetch/api/basic/request-head.any.worker.html", {…}] ]
    │                         ↑ one entry per URL the source expands into;
    │                           {…} holds timeout, script deps, variants
    ├── reftest        ← [url, [[reference_url, "=="|"!="]], {…}]
    ├── print-reftest
    ├── crashtest
    ├── manual         ← needs a human; not run by the census
    └── support        ← helpers, handlers, resources (never run)
```

Read it with plain `json`: walk `items[type]` as a tree of dicts until you
reach a list; element 0 is the source hash, each further element is
`[url, extras]` (`url` is `null` when it equals the source path).
`netcensus/manifest.py` does exactly this.
"""


def render(census_json: str, subtree_manifest: str | None = None,
           depth: int = 2, top_n: int = 0) -> str:
    with open(census_json, encoding="utf-8") as fh:
        census = json.load(fh)
    rows = census["rows"]

    by_top: dict = collections.defaultdict(lambda: [0, 0])
    by_sub: dict = collections.defaultdict(lambda: [0, 0])
    by_type: dict = collections.defaultdict(lambda: [0, 0])
    for r in rows:
        parts = r["url"].strip("/").split("/")
        top = parts[0]
        sub = "/".join(parts[:depth]) if len(parts) > depth else top
        for bucket, key in ((by_top, top), (by_sub, sub), (by_type, r["type"])):
            bucket[key][0] += 1
            bucket[key][1] += 1 if r["emitted"] else 0

    n, e = census["n_tests"], census["n_emitted"]
    out = []
    out.append("# Test tree & manifest tree\n")
    out.append(f"Generated from `{os.path.basename(census_json)}` "
               f"({census['generated']}), Chrome headless, stock flags.\n")
    out.append("| | count |\n|---|---:|")
    out.append(f"| tests run | {n} |")
    out.append(f"| **emit network** (seen in access log) | **{e}** "
               f"({100.0*e/max(1,n):.1f}%) |")
    out.append(f"| attempted, never reached server | {census['n_cdp_only']} |")
    out.append(f"| errors | {census['n_errors']} |")
    out.append(f"| unattributed log lines | {census['orphan_requests']} |\n")

    out.append("## By test type\n")
    out.append("| type | run | emit |\n|---|---:|---:|")
    for k, (a, b) in sorted(by_type.items(), key=lambda kv: -kv[1][1]):
        out.append(f"| {k} | {a} | {b} |")
    out.append("")

    out.append("## Test tree (emitting tests per directory)\n")
    out.append("Top-level directories, most emitting tests first; the second "
               "level is shown for directories with 25+ emitting tests.\n")
    out.append("```text")
    tops = sorted(((k, v) for k, v in by_top.items() if v[1]),
                  key=lambda kv: (-kv[1][1], kv[0]))
    if top_n:
        tops = tops[:top_n]
    for i, (top, (a, b)) in enumerate(tops):
        last = i == len(tops) - 1
        out.append(f"{'└──' if last else '├──'} {top + '/':<34}"
                   f"{b:>6} / {a:<6} emit")
        if b >= 25:
            subs = sorted(((k, v) for k, v in by_sub.items()
                           if k.startswith(top + "/") and v[1]),
                          key=lambda kv: (-kv[1][1], kv[0]))[:8]
            for j, (sk, (sa, sb)) in enumerate(subs):
                bar = "    " if last else "│   "
                elbow = "└──" if j == len(subs) - 1 else "├──"
                name = sk.split("/", 1)[1] + "/"
                out.append(f"{bar}{elbow} {name:<30}{sb:>6} / {sa:<6}")
    zero = sorted(k for k, v in by_top.items() if not v[1])
    out.append("```\n")
    if zero:
        out.append(f"<details><summary>{len(zero)} directories with no "
                   f"emitting test</summary>\n\n" + ", ".join(f"`{z}`" for z in zero)
                   + "\n\n</details>\n")

    out.append("## Manifest tree\n")
    out.append(MANIFEST_SHAPE)
    if subtree_manifest and os.path.exists(subtree_manifest):
        with open(subtree_manifest, encoding="utf-8") as fh:
            m = json.load(fh)
        out.append("Entries in the pruned tree's own `MANIFEST.json`:\n")
        out.append("| type | source files |\n|---|---:|")
        for kind, tree in sorted(m.get("items", {}).items()):
            out.append(f"| {kind} | {_count_leaves(tree)} |")
        out.append("")
    return "\n".join(out) + "\n"


def _count_leaves(node) -> int:
    if isinstance(node, dict):
        return sum(_count_leaves(v) for v in node.values())
    return 1 if isinstance(node, list) else 0
