"""Tiny HTTP server: static UI + JSON API. Standard library only."""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .controller import Controller

log = logging.getLogger("splitflap")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "static")


def _bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("1", "true", "yes", "on")


def make_handler(ctrl: Controller):
    class Handler(BaseHTTPRequestHandler):
        server_version = "splitflap-web"

        def log_message(self, fmt, *args):
            log.debug("%s - %s", self.address_string(), fmt % args)

        # --- helpers ---
        def _send(self, code: int, body: bytes, content_type: str, cache: bool = False):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=300" if cache else "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, code: int = 200):
            self._send(code, json.dumps(data).encode("utf-8"), "application/json")

        def _params(self) -> dict:
            query = {k: v[-1] for k, v in parse_qs(urlparse(self.path).query).items()}
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                raw = self.rfile.read(length).decode("utf-8")
                ctype = self.headers.get("Content-Type", "")
                if "json" in ctype:
                    query.update(json.loads(raw or "{}"))
                else:
                    query.update({k: v[-1] for k, v in parse_qs(raw).items()})
            return query

        def _static(self, path: str):
            if path in ("", "/"):
                path = "/index.html"
            root = os.path.realpath(STATIC_DIR)
            full = os.path.realpath(os.path.join(root, path.lstrip("/")))
            if not full.startswith(root + os.sep) or not os.path.isfile(full):
                return self._send(404, b"not found", "text/plain")
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            with open(full, "rb") as f:
                self._send(200, f.read(), ctype)

        # --- routing ---
        def do_GET(self):
            path = urlparse(self.path).path
            try:
                if path == "/api/state":
                    return self._json(ctrl.state())
                if path == "/api/scan":
                    return self._json(ctrl.scan())
                if path == "/api/display":  # handy for curl / iOS Shortcuts
                    return self._display(self._params())
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            return self._static(path)

        def do_POST(self):
            path = urlparse(self.path).path
            try:
                p = self._params()
                if path == "/api/display":
                    return self._display(p)
                if path == "/api/clear":
                    ctrl.clear(p.get("animation", "start"), int(p.get("slowdown", 1)))
                elif path == "/api/resync":
                    ctrl.resync()
                elif path == "/api/stop":
                    ctrl.stop()
                elif path == "/api/mode":
                    ctrl.set_mode(p.get("mode", "text"), p.get("format"))
                elif path == "/api/location":
                    place = ctrl.set_location(p.get("query", ""))
                    return self._json({"ok": True, "location": place})
                elif path == "/api/presets":
                    ctrl.add_preset(p.get("text", ""))
                elif path == "/api/presets/delete":
                    ctrl.remove_preset(p.get("text", ""))
                else:
                    return self._json({"ok": False, "error": "unknown endpoint"}, 404)
                return self._json({"ok": True})
            except (ValueError, TypeError) as e:
                return self._json({"ok": False, "error": str(e)}, 400)

        def _display(self, p: dict):
            lines = p.get("lines")
            if isinstance(lines, str):
                lines = lines.split("\n")
            shown = ctrl.show(
                text=p.get("text"),
                lines=lines,
                animation=p.get("animation", "start"),
                same=_bool(p.get("same", False)),
                wipe=_bool(p.get("wipe", False)),
                slowdown=int(p.get("slowdown", 1)),
            )
            return self._json({"ok": True, "shown": shown})

    return Handler


class DualStackServer(ThreadingHTTPServer):
    """Listens on IPv6 and IPv4 at once. Needed because <hostname>.local
    also resolves to the Pi's IPv6 address, which browsers try first."""

    address_family = socket.AF_INET6

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except (AttributeError, OSError):
            pass
        super().server_bind()


def make_server(host: str, port: int, handler) -> ThreadingHTTPServer:
    if host in ("", "0.0.0.0", "::") and socket.has_ipv6:
        try:
            return DualStackServer(("::", port), handler)
        except OSError as e:
            log.warning("IPv6 not available (%s), listening on IPv4 only", e)
    return ThreadingHTTPServer((host, port), handler)


def serve(ctrl: Controller, host: str, port: int) -> None:
    httpd = make_server(host, port, make_handler(ctrl))
    httpd.daemon_threads = True
    log.info("split-flap web app on port %d, IPv4 + IPv6 (%s)", port, ctrl.hardware_kind)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
