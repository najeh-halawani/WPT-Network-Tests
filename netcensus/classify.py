"""The one place the "does this test emit network requests?" decision is made."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from . import accesslog
from .browser import Visit
from .manifest import Test

_NETWORK_SCHEMES = ("http://", "https://", "ws://", "wss://")


@dataclass
class TestResult:
    url: str
    type: str
    # THE ANSWER: at least one request of the test's own reached wptserve.
    emitted: bool = False
    n_logged: int = 0
    logged: list = field(default_factory=list)     # "GET :8000/path", sorted
    statuses: list = field(default_factory=list)
    # Context for reading the answer -- not part of it.
    n_cdp: int = 0              # requests the browser attempted (non-noise)
    cdp_only: bool = False      # attempted, but the server never saw any
    completed: bool = False     # testharness reported
    targets: int = 0            # page + frames + workers attached
    seconds: float = 0.0        # time until the harness reported
    error: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        if d["error"] is None:
            del d["error"]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "TestResult":
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)


def classify(test: Test, visit: Visit, logged: list) -> TestResult:
    """Combine one test's two observations into its row.

    `emitted` is the access log's answer and nothing else.  The browser census
    can only add context: it reports what was ATTEMPTED, and an attempt that was
    blocked, served from cache or aimed at a non-WPT host never hit the server.
    """
    self_paths = accesslog.self_paths(test.url)
    own = [r for r in accesslog.dedup(logged)
           if not accesslog.is_noise(r["path"], self_paths)]
    attempted = [r for r in visit.requests
                 if r["url"].startswith(_NETWORK_SCHEMES)
                 and not accesslog.is_noise(accesslog.url_path(r["url"]),
                                            self_paths)]
    return TestResult(
        url=test.url,
        type=test.type,
        emitted=bool(own),
        n_logged=len(own),
        logged=sorted({f'{r["method"]} :{r["port"]}{r["path"]}'
                       for r in own}),
        statuses=sorted({r["status"] for r in own if r["status"]}),
        n_cdp=len(attempted),
        cdp_only=bool(attempted) and not own,
        completed=visit.completed,
        targets=visit.targets,
        seconds=visit.seconds,
        error=visit.error,
    )
