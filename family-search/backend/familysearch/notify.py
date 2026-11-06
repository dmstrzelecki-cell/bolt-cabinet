"""Parent notifications for flagged activity.

The destination is NOTIFY_URL in the root-only env file: an ntfy topic URL
(self-hosted on the LAN, or ntfy.sh), or any webhook that accepts JSON (Home
Assistant, Discord, Slack-style). The URL itself acts as a secret.

Privacy default: the message says only THAT something was flagged, and links
to /parent. Her search text is included only if a parent turns that on, since
a hosted push service is a third party.

Bursts are coalesced: at most one message per COOLDOWN seconds; anything in
between is summed into the next message.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

import httpx

COOLDOWN = 300.0
KIND_TEXT = {
    "blocked": "a blocked search",
    "watch": "a search with a watch word",
    "request": "an Ask-a-grown-up request",
    "test": "a test notification",
}


def _send_http(url: str, fmt: str, title: str, body: str, click: str) -> None:
    with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as c:
        if fmt == "ntfy":
            r = c.post(url, content=body.encode(), headers={
                "Title": title, "Tags": "warning", "Priority": "high", "Click": click})
        else:  # generic JSON webhook: Discord uses "content", Slack "text", HA "message"
            r = c.post(url, json={"title": title, "message": body, "content": f"**{title}**\n{body}",
                                  "text": f"{title}\n{body}", "url": click})
        r.raise_for_status()


class Notifier:
    def __init__(self, url: str | None, fmt: str, parent_url: str,
                 send: Callable[[str, str, str, str, str], None] = _send_http,
                 clock: Callable[[], float] = time.monotonic, background: bool = True):
        self.url = url
        self.fmt = fmt if fmt in ("ntfy", "json") else "ntfy"
        self.parent_url = parent_url
        self._send = send
        self._clock = clock
        self._background = background
        self._lock = threading.Lock()
        self._last = -1e9
        self._pending: dict[str, int] = {}
        self._pending_text: list[str] = []
        self._timer: threading.Timer | None = None
        self.last_error: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.url)

    def notify(self, kind: str, text: str | None = None, force: bool = False) -> bool:
        """Queue a notification. Returns True if one was sent (or scheduled) now."""
        if not self.url:
            return False
        with self._lock:
            self._pending[kind] = self._pending.get(kind, 0) + 1
            if text:
                self._pending_text.append(text[:120])
            wait = COOLDOWN - (self._clock() - self._last)
            if force or wait <= 0:
                return self._flush_locked()
            if self._timer is None and self._background:
                self._timer = threading.Timer(wait, self._flush_from_timer)
                self._timer.daemon = True
                self._timer.start()
            return False

    def _flush_from_timer(self) -> None:
        with self._lock:
            self._timer = None
            self._flush_locked()

    def flush(self) -> bool:
        with self._lock:
            return self._flush_locked()

    def _flush_locked(self) -> bool:
        if not self._pending:
            return False
        counts, texts = self._pending, self._pending_text
        self._pending, self._pending_text = {}, []
        self._last = self._clock()
        parts = [f"{n} × {KIND_TEXT.get(k, k)}" if n > 1 else KIND_TEXT.get(k, k)
                 for k, n in counts.items()]
        title = "Family Search: " + ("test" if list(counts) == ["test"] else "needs a look")
        what = ", ".join(parts)
        body = f"{what[:1].upper()}{what[1:]} at {time.strftime('%-I:%M %p')}."
        if texts:
            body += "\n" + "\n".join(f"• {t}" for t in texts[:10])
        body += f"\nOpen {self.parent_url}"
        args = (self.url, self.fmt, title, body, self.parent_url)
        if self._background:
            threading.Thread(target=self._deliver, args=args, daemon=True).start()
        else:
            self._deliver(*args)
        return True

    def _deliver(self, *args) -> None:
        try:
            self._send(*args)
            self.last_error = None
        except Exception as e:  # never log the URL: it is a secret
            self.last_error = f"{type(e).__name__}"
