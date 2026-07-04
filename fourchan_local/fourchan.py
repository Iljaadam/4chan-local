"""Minimal 4chan read-only API client.

Endpoints used:
  https://a.4cdn.org/{board}/threads.json        -> all live threads + last_modified
  https://a.4cdn.org/{board}/thread/{no}.json     -> full thread (OP + replies)

Rules honored: global rate cap (<=1 req/s by default), If-Modified-Since via
per-resource ETag/Last-Modified, polite User-Agent. Media bytes live on i.4cdn.org
and are intentionally NOT fetched here (text-only POC).
"""
import threading
import time

import httpx

API = "https://a.4cdn.org"
MEDIA = "https://i.4cdn.org"   # file + thumbnail CDN
UA = "4ch-archive/0.1 (personal archival POC; respects 1req/s)"


class RateLimiter:
    """Global minimum-interval limiter shared across all calls."""

    def __init__(self, req_per_sec: float):
        self._min_interval = 1.0 / max(req_per_sec, 0.001)
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            if now < self._next_allowed:
                time.sleep(self._next_allowed - now)
            self._next_allowed = time.monotonic() + self._min_interval


class FourChan:
    def __init__(self, req_per_sec: float = 1.0):
        self._rl = RateLimiter(req_per_sec)
        self._client = httpx.Client(
            headers={"User-Agent": UA},
            timeout=30.0,
            follow_redirects=True,
        )
        # resource_url -> Last-Modified header, for conditional GETs
        self._last_mod: dict[str, str] = {}

    def _get(self, path: str):
        """Conditional GET. Returns parsed JSON, or None on 304/404."""
        url = f"{API}{path}"
        headers = {}
        if url in self._last_mod:
            headers["If-Modified-Since"] = self._last_mod[url]
        self._rl.wait()
        resp = self._client.get(url, headers=headers)
        if resp.status_code in (304, 404):
            return None
        resp.raise_for_status()
        if "Last-Modified" in resp.headers:
            self._last_mod[url] = resp.headers["Last-Modified"]
        return resp.json()

    def boards(self):
        """All boards: list of {board, title, ws_board(1=worksafe)}."""
        data = self._get("/boards.json")
        if data is None:
            return []
        return data.get("boards", [])

    def threads(self, board: str):
        """List of {no, last_modified, replies} for every live thread on the board.

        Returns None if unchanged since last call (304).
        """
        data = self._get(f"/{board}/threads.json")
        if data is None:
            return None
        out = []
        for page in data:
            for t in page.get("threads", []):
                out.append(t)
        return out

    def thread(self, board: str, thread_no: int):
        """Full thread posts list, or None if 404/unchanged."""
        data = self._get(f"/{board}/thread/{thread_no}.json")
        if data is None:
            return None
        return data.get("posts", [])

    def fetch_bytes(self, board: str, name: str):
        """Download a media file/thumbnail from i.4cdn.org. `name` is e.g.
        '1696970000000.jpg' (full) or '1696970000000s.jpg' (thumbnail).
        Returns raw bytes, or None when the file is gone. Rate-limited like API
        calls. 4chan serves 403/410 (not just 404) for expired/removed media, so
        all three count as "gone" — otherwise a single old file raises and wedges
        the whole worker on it batch after batch."""
        url = f"{MEDIA}/{board}/{name}"
        self._rl.wait()
        resp = self._client.get(url)
        if resp.status_code in (403, 404, 410):
            return None
        resp.raise_for_status()
        return resp.content

    def close(self):
        self._client.close()
