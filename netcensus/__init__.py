"""netcensus -- which Web Platform Tests actually put a request on the wire.

Runs stock WPT tests in stock headless Chrome against `wpt serve --verbose`,
and decides per test from wptserve's ACCESS LOG whether the test emitted any
network request of its own (harness boilerplate filtered out).  The browser's
own request census (CDP) is recorded alongside as a second, independent view.

    python -m netcensus verify fetch/api/basic/request-head.any.html
    python -m netcensus census --filter fetch/ --jobs 8
    python -m netcensus subtree data/census.json --out tree/
    python -m netcensus tree data/census.json > TREE.md
"""
__version__ = "1.0.0"
