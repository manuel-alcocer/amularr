"""Threaded HTTP front end routing requests to the Torznab and
qBittorrent facades. Standard library only."""

from __future__ import annotations

import email.parser
import email.policy
import logging
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

from . import __version__
from .config import Config
from .ec import ECClient, ECError
from .qbittorrent import QBittorrentAPI
from .torznab import TorznabAPI
from .web import Response

log = logging.getLogger(__name__)

TORZNAB_PATHS = ("/api", "/torznab/api", "/api/torznab", "/torznab")


def parse_form(content_type: str, body: bytes) -> dict[str, str]:
    """Parse application/x-www-form-urlencoded or multipart/form-data."""
    if not body:
        return {}
    main_type = content_type.split(";", 1)[0].strip().lower()
    if main_type == "multipart/form-data":
        raw = b"Content-Type: " + content_type.encode("latin-1") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
        message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(raw)
        form: dict[str, str] = {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if not name:
                continue
            payload = part.get_payload(decode=True) or b""
            form[str(name)] = payload.decode("utf-8", "replace")
        return form
    return dict(parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True))


class Bridge:
    """Holds the shared services used by every request."""

    def __init__(self, config: Config, ec: ECClient, torznab: TorznabAPI, qbittorrent: QBittorrentAPI):
        self.config = config
        self.ec = ec
        self.torznab = torznab
        self.qbittorrent = qbittorrent

    def dispatch(self, method: str, path: str, query: dict[str, str], form: dict[str, str], cookies: dict[str, str]) -> Response:
        if path.startswith("/api/v2/"):
            return self.qbittorrent.handle(method, path, query, form, cookies)
        if path.rstrip("/") in TORZNAB_PATHS:
            return self.torznab.handle({**query, **form})
        if path == "/ping":
            return Response.text("pong")
        if path in ("/", "/health", "/healthz"):
            return self.health()
        return Response.text("Not Found", 404)

    def health(self) -> Response:
        info = {"name": "amularr", "version": __version__, "amule": None, "ok": False}
        try:
            state = self.ec.conn_state()
            info["amule"] = {
                "version": self.ec.server_version,
                "ed2k_connected": state.ed2k_connected,
                "kad_connected": state.kad_connected,
                "server": state.server_name,
            }
            info["ok"] = True
        except ECError as exc:
            info["error"] = str(exc)
        return Response.json(info, 200 if info["ok"] else 503)


class Handler(BaseHTTPRequestHandler):
    server_version = f"amularr/{__version__}"
    bridge: Bridge  # set on the server class

    def log_message(self, fmt, *args):  # route to logging instead of stderr
        log.debug("%s " + fmt, self.address_string(), *args)

    def _handle(self, method: str) -> None:
        parts = urlsplit(self.path)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        body = b""
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            body = self.rfile.read(length)
        form = parse_form(self.headers.get("Content-Type", ""), body) if method == "POST" else {}
        cookie = SimpleCookie()
        cookie.load(self.headers.get("Cookie", ""))
        cookies = {k: v.value for k, v in cookie.items()}
        try:
            response = self.server.bridge.dispatch(method, parts.path, query, form, cookies)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - last-resort handler
            log.exception("unhandled error serving %s %s", method, self.path)
            response = Response.text("Internal Server Error", 500)
        log.info("%s %s -> %d", method, self.path if len(self.path) < 200 else self.path[:200] + "...", response.status)
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        for name, value in response.headers:
            self.send_header(name, value)
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(response.body)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_HEAD(self):
        self._handle("HEAD")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, bridge: Bridge):
        self.bridge = bridge
        super().__init__((bridge.config.listen_host, bridge.config.listen_port), Handler)
