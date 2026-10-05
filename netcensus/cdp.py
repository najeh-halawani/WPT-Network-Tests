"""A minimal Chrome DevTools Protocol client: one socket, flat sessions."""
from __future__ import annotations

import collections
import json
import threading
import time

from websockets.sync.client import connect as ws_connect


class CDPError(RuntimeError):
    pass


class CDP:
    """Command replies are routed by id; events are buffered for the caller.

    The event buffer is BOUNDED.  With Network.enable on, a caller that stops
    draining otherwise grows it until the process dies and the socket closes --
    which downstream does not look like a crash, it looks like thousands of
    tests that instantly reported no traffic.
    """

    def __init__(self, ws_url: str, max_events: int = 20000):
        self.ws = ws_connect(ws_url, max_size=64 * 1024 * 1024, open_timeout=15)
        self._id = 0
        self._send_lock = threading.Lock()
        self._replies: dict[int, dict] = {}
        self._discard: set = set()           # ids sent with post()
        self._events: collections.deque = collections.deque(maxlen=max_events)
        self._cv = threading.Condition()
        self._alive = True
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        try:
            for raw in self.ws:
                msg = json.loads(raw)
                with self._cv:
                    if "id" in msg:
                        if msg["id"] in self._discard:
                            self._discard.discard(msg["id"])
                        else:
                            self._replies[msg["id"]] = msg
                    else:
                        self._events.append(msg)
                    self._cv.notify_all()
        except Exception:
            pass
        finally:
            with self._cv:
                self._alive = False
                self._cv.notify_all()

    @property
    def alive(self) -> bool:
        return self._alive

    def send(self, method: str, params: dict | None = None,
             session: str | None = None, timeout: float = 30) -> dict:
        # id allocation and the write are one critical section: interleaved
        # frames from two threads would corrupt the stream.
        with self._send_lock:
            self._id += 1
            mid = self._id
            msg = {"id": mid, "method": method, "params": params or {}}
            if session:
                msg["sessionId"] = session
            self.ws.send(json.dumps(msg))
        deadline = time.time() + timeout
        with self._cv:
            while mid not in self._replies:
                if not self._alive:
                    raise CDPError("connection closed")
                left = deadline - time.time()
                if left <= 0:
                    raise TimeoutError(f"{method} timed out")
                self._cv.wait(timeout=min(left, 0.5))
            reply = self._replies.pop(mid)
        if "error" in reply:
            raise CDPError(f"{method}: {reply['error']}")
        return reply.get("result", {})

    def post(self, method: str, params: dict | None = None,
             session: str | None = None) -> None:
        """Send without waiting for the reply (it is discarded when it comes).

        For setup sent to a target that may be PAUSED: a paused worker answers
        nothing until it is resumed, so waiting on each command in turn costs a
        full timeout apiece.  The protocol handles one session's messages in
        order, so commands posted before `runIfWaitingForDebugger` still take
        effect before the target runs.
        """
        with self._send_lock:
            self._id += 1
            self._discard.add(self._id)
            msg = {"id": self._id, "method": method, "params": params or {}}
            if session:
                msg["sessionId"] = session
            try:
                self.ws.send(json.dumps(msg))
            except Exception:
                pass

    def try_send(self, method: str, params: dict | None = None,
                 session: str | None = None, timeout: float = 8) -> dict | None:
        try:
            return self.send(method, params, session, timeout)
        except (CDPError, TimeoutError):
            return None

    def drain(self) -> list[dict]:
        with self._cv:
            evs = list(self._events)
            self._events.clear()
        return evs

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass
