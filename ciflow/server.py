"""ci-flowd: the collector daemon.

Runs the collectors and serves one snapshot of the CI machine on 127.0.0.1:

  GET  /api/snapshot  the snapshot as JSON
  GET  /api/events    server-sent events, one snapshot per second
  POST /api/refresh   collect now instead of waiting for the next interval
  GET  /api/whoami    the viewer's Tailscale identity, when served through
                      `tailscale serve`
  GET  /              the web dashboard
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, install, notify
from .collect import HOST, Collector, ConfigError

BIND = "127.0.0.1"
PORT = int(os.environ.get("CIFLOW_PORT", "7340"))
EVENT_INTERVAL = 1.0
INDEX = os.path.join(os.path.dirname(__file__), "web", "index.html")


def make_server(source, port=PORT, bind=BIND, interval=EVENT_INTERVAL):
    """An HTTP server over `source`, anything with snapshot() and refresh()."""
    stopping = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def header(self, name):
            """A request header as text; http.server decodes raw bytes as latin-1."""
            v = self.headers.get(name)
            return v.encode("latin-1").decode("utf-8", "replace") if v else None

        def send_body(self, code, ctype, body):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/snapshot":
                self.send_body(200, "application/json", json.dumps(source.snapshot()).encode())
            elif path == "/api/events":
                self.events()
            elif path == "/api/whoami":
                # tailscale serve adds these for requests from the tailnet.
                who = {"login": self.header("Tailscale-User-Login"),
                       "name": self.header("Tailscale-User-Name")}
                self.send_body(200, "application/json", json.dumps(who).encode())
            elif path in ("/", "/index.html"):
                with open(INDEX, "rb") as f:
                    self.send_body(200, "text/html; charset=utf-8", f.read())
            else:
                self.send_body(404, "text/plain", b"not found\n")

        def do_POST(self):
            if self.path.split("?", 1)[0] == "/api/refresh":
                source.refresh()
                self.send_body(202, "application/json", b'{"ok": true}')
            else:
                self.send_body(404, "text/plain", b"not found\n")

        def events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                while not stopping.is_set():
                    self.wfile.write(b"data: " + json.dumps(source.snapshot()).encode() + b"\n\n")
                    self.wfile.flush()
                    stopping.wait(interval)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer((bind, port), Handler)
    server.daemon_threads = True
    server.stopping = stopping
    return server


def main():
    install.hold()
    try:
        conf = config.load()
        collector = Collector(config.fleets(conf), config_path=config.CONFIG)
        alerts = notify.settings(conf)
    except (ConfigError, notify.NotifyError) as e:
        sys.exit(f"ci-flowd: {e}")
    collector.start()
    if alerts:
        notify.watch(collector, notify.Notifier(alerts["ntfy"], alerts["level"], HOST), collector.stopped)
        print(f"ci-flowd: notifying {alerts['level']} problems to {notify.redact(alerts['ntfy'])}", flush=True)
    try:
        server = make_server(collector)
    except OSError as e:
        sys.exit(f"ci-flowd: cannot listen on {BIND}:{PORT} ({e})")
    print(f"ci-flowd: serving on http://{BIND}:{server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stopping.set()
        server.server_close()
