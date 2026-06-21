"""HTTP log server for the ``dexim launch`` TUI session.

Provides a lightweight built-in HTTP server (stdlib only, no extra
dependencies) that serves a live log viewer page with Server-Sent Events
(SSE) for real-time streaming.

Endpoints
---------
``GET /``
    HTML page with auto-connecting SSE log viewer.
``GET /api/log/tail?n=100``
    Return the last *n* log entries as JSON.
``GET /api/log/stream``
    SSE endpoint — pushes new log entries as they arrive.

Usage::

    from dexim_manus_brainco.cli.log_server import LogServer

    server = LogServer(port=8090)
    server.start()
    # ... session runs ...
    server.emit({"ts": "12:03:01", "level": "info", "msg": "Node started"})
    server.stop()
"""

from __future__ import annotations

import json
import queue
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

# ---------------------------------------------------------------------------
# Log event store (thread-safe ring buffer)
# ---------------------------------------------------------------------------


class LogStore:
    """Thread-safe ring buffer for structured log events.

    Keeps the last *max_entries* events in memory and notifies SSE
    listeners of new arrivals.
    """

    def __init__(self, max_entries: int = 500) -> None:
        self._buffer: deque[dict[str, Any]] = deque(maxlen=max_entries)
        self._lock = threading.Lock()
        self._sse_queues: list[queue.Queue[dict[str, Any] | None]] = []
        self._sse_lock = threading.Lock()

    def append(self, event: dict[str, Any]) -> None:
        """Add a log event and notify all SSE listeners.

        Args:
            event: A JSON-serialisable dict with at least ``ts``, ``level``,
                and ``msg`` keys.
        """
        with self._lock:
            self._buffer.append(event)
        # Notify SSE listeners
        with self._sse_lock:
            for q in self._sse_queues:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    pass

    def tail(self, n: int = 100) -> list[dict[str, Any]]:
        """Return the last *n* events.

        Args:
            n: Maximum number of events to return.

        Returns:
            List of event dicts, oldest first.
        """
        with self._lock:
            items = list(self._buffer)
        return items[-n:] if n > 0 else items

    def subscribe(self) -> queue.Queue[dict[str, Any] | None]:
        """Create a new SSE subscriber queue.

        Returns:
            A :class:`queue.Queue` that receives new events.  The caller
            should check for ``None`` as a sentinel (server shutdown).
        """
        q: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=256)
        with self._sse_lock:
            self._sse_queues.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[dict[str, Any] | None]) -> None:
        """Remove an SSE subscriber queue.

        Args:
            q: The queue to remove.
        """
        with self._sse_lock:
            try:
                self._sse_queues.remove(q)
            except ValueError:
                pass

    def shutdown(self) -> None:
        """Send sentinel ``None`` to all SSE subscribers and clear queues."""
        with self._sse_lock:
            for q in self._sse_queues:
                try:
                    q.put_nowait(None)
                except queue.Full:
                    pass
            self._sse_queues.clear()


# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------

_HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DexImitate · Live Log</title>
<style>
  :root {
    --bg: #0d1117;
    --fg: #c9d1d9;
    --muted: #8b949e;
    --border: #30363d;
    --info: #58a6ff;
    --success: #3fb950;
    --warn: #d2991d;
    --error: #f85149;
    --brand: #a371f7;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    font: 13px/1.5 'Cascadia Code','Consolas','Monaco',monospace;
    background: var(--bg); color: var(--fg);
    display:flex; flex-direction:column; height:100vh;
  }
  header {
    padding:8px 16px; border-bottom:1px solid var(--border);
    display:flex; justify-content:space-between; align-items:center;
    background:#161b22;
  }
  header h1 { font-size:14px; color:var(--brand); }
  header .status { font-size:12px; }
  header .status .dot { display:inline-block; width:8px; height:8px;
    border-radius:50%; margin-right:4px; }
  header .status.connected .dot { background:var(--success); }
  header .status.disconnected .dot { background:var(--error); }
  #log {
    flex:1; overflow-y:auto; padding:8px 16px;
    font-size:12px; line-height:1.6;
  }
  .line { white-space:pre-wrap; word-break:break-all; }
  .line.info  { color:var(--info); }
  .line.success { color:var(--success); }
  .line.warn  { color:var(--warn); }
  .line.error { color:var(--error); }
  .line.debug { color:var(--muted); }
  .ts { color:var(--muted); margin-right:8px; }
  .node { color:var(--brand); margin-right:8px; }
</style>
</head>
<body>
<header>
  <h1>DexImitate · Launch Log</h1>
  <span id="status" class="status disconnected">
    <span class="dot"></span>connecting...
  </span>
</header>
<div id="log"></div>
<script>
  const logEl = document.getElementById('log');
  const statusEl = document.getElementById('status');
  const TAIL_N = 200;

  // Load initial tail
  fetch('/api/log/tail?n=' + TAIL_N)
    .then(r => r.json())
    .then(events => { for (const e of events) appendLine(e); })
    .catch(() => {});

  // SSE stream
  const es = new EventSource('/api/log/stream');
  es.onopen = () => {
    statusEl.className = 'status connected';
    statusEl.innerHTML = '<span class="dot"></span>live';
  };
  es.onerror = () => {
    statusEl.className = 'status disconnected';
    statusEl.innerHTML = '<span class="dot"></span>disconnected';
  };
  es.addEventListener('log', e => {
    try { appendLine(JSON.parse(e.data)); } catch(_) {}
  });

  function appendLine(ev) {
    const div = document.createElement('div');
    div.className = 'line ' + (ev.level || 'info');
    div.innerHTML = '<span class="ts">' + esc(ev.ts||'') + '</span>' +
      (ev.node ? '<span class="node">' + esc(ev.node) + '</span>' : '') +
      esc(ev.msg||'');
    logEl.appendChild(div);
    // Auto-scroll if near bottom
    if (logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 40) {
      logEl.scrollTop = logEl.scrollHeight;
    }
  }
  function esc(s) {
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }
</script>
</body>
</html>"""


class _LogHandler(BaseHTTPRequestHandler):
    """HTTP request handler serving the log viewer page and API."""

    # Class-level references set by LogServer.start()
    log_store: LogStore | None = None

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default access logging to stderr."""
        pass

    def do_GET(self) -> None:
        path = self.path.split("?")[0]

        if path == "/":
            self._serve_html()
        elif path == "/api/log/tail":
            self._serve_tail()
        elif path == "/api/log/stream":
            self._serve_sse()
        else:
            self.send_error(404)

    def _serve_html(self) -> None:
        body = _HTML_PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_tail(self) -> None:
        from urllib.parse import parse_qs, urlparse

        store = _LogHandler.log_store
        if store is None:
            self.send_error(503, "Log store not ready")
            return

        qs = parse_qs(urlparse(self.path).query)
        try:
            n = int(qs.get("n", ["100"])[0])
        except (ValueError, IndexError):
            n = 100

        events = store.tail(n)
        body = json.dumps(events, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_sse(self) -> None:
        store = _LogHandler.log_store
        if store is None:
            self.send_error(503, "Log store not ready")
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        q = store.subscribe()
        try:
            while True:
                event = q.get()
                if event is None:  # shutdown sentinel
                    break
                data = json.dumps(event, ensure_ascii=False)
                self.wfile.write(f"event: log\ndata: {data}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            store.unsubscribe(q)


# ---------------------------------------------------------------------------
# Log server
# ---------------------------------------------------------------------------


class LogServer:
    """Lightweight HTTP server for live log viewing.

    Runs in a daemon thread.  No extra pip packages needed — uses only
    stdlib ``http.server``.

    Args:
        port: TCP port to listen on (default 8090).
        max_events: Max log events kept in the ring buffer.
    """

    def __init__(self, port: int = 8090, max_events: int = 2000) -> None:
        self._port = port
        self._store = LogStore(max_entries=max_events)
        self._httpd: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def store(self) -> LogStore:
        """The shared :class:`LogStore` for emitting events."""
        return self._store

    @property
    def port(self) -> int:
        """The TCP port the server listens on."""
        return self._port

    @property
    def url(self) -> str:
        """The base HTTP URL for the log viewer."""
        return f"http://localhost:{self._port}"

    def start(self) -> None:
        """Start the HTTP server in a daemon thread."""
        # Inject the log store into the handler class
        _LogHandler.log_store = self._store  # type: ignore[assignment]

        self._httpd = HTTPServer(("0.0.0.0", self._port), _LogHandler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        # Brief pause to let the socket bind
        time.sleep(0.1)

    def stop(self) -> None:
        """Shut down the HTTP server and close SSE connections."""
        if self._httpd is not None:
            self._store.shutdown()
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def emit(self, event: dict[str, Any]) -> None:
        """Push a log event to the store and notify SSE clients.

        Args:
            event: Dict with keys like ``ts``, ``level``, ``node``, ``msg``.
        """
        self._store.append(event)
