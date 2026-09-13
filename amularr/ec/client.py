"""Synchronous client for the aMule External Connections protocol."""

from __future__ import annotations

import hashlib
import logging
import socket
import threading
from dataclasses import dataclass

from . import codes as C
from .tag import HEADER, ECProtocolError, Packet, Tag, decode_body, encode_frame

log = logging.getLogger(__name__)


class ECError(Exception):
    """Raised when the core answers OP_FAILED or the handshake is refused."""


class ECConnectionError(ECError):
    """Raised on socket failures."""


@dataclass
class SearchResult:
    ecid: int
    hash: str
    name: str
    size: int
    sources: int
    complete_sources: int
    status: int  # CSearchFile download status; 0 == NEW (not already known)

    @property
    def ed2k_link(self) -> str:
        from urllib.parse import quote

        return f"ed2k://|file|{quote(self.name, safe='')}|{self.size}|{self.hash}|/"


@dataclass
class QueuedFile:
    ecid: int
    hash: str
    name: str
    size: int
    done: int
    transferred: int
    speed: int
    status: int
    stopped: bool
    sources: int
    sources_xfer: int
    category: int
    part_met: str
    last_seen_complete: int
    ed2k_link: str

    @property
    def status_name(self) -> str:
        return C.PS_NAMES.get(self.status, str(self.status))

    @property
    def progress(self) -> float:
        return self.done / self.size if self.size else 0.0

    @property
    def is_complete(self) -> bool:
        return self.status == C.PS_COMPLETE


@dataclass
class ConnState:
    ed2k_connected: bool
    ed2k_connecting: bool
    kad_connected: bool
    kad_firewalled: bool
    kad_running: bool
    ed2k_id: int
    client_id: int
    server_name: str


class ECClient:
    """Blocking EC client. Safe to share across threads: every request
    holds a lock for the duration of the round trip."""

    def __init__(
        self,
        host: str,
        port: int = 4712,
        password: str | None = None,
        password_md5: str | None = None,
        client_name: str = "amularr",
        client_version: str = "0.1",
        timeout: float = 30.0,
        version_id: str | None = None,
    ):
        if password_md5 is None:
            if password is None:
                raise ValueError("password or password_md5 is required")
            password_md5 = hashlib.md5(password.encode("utf-8")).hexdigest()
        self.host = host
        self.port = port
        self._password_md5 = password_md5.lower()
        self.client_name = client_name
        self.client_version = client_version
        self.timeout = timeout
        self.version_id = version_id
        self._sock: socket.socket | None = None
        self._lock = threading.RLock()
        self.server_version = ""

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def connect(self) -> None:
        with self._lock:
            self.close()
            try:
                sock = socket.create_connection((self.host, self.port), self.timeout)
                sock.settimeout(self.timeout)
            except OSError as exc:
                raise ECConnectionError(f"cannot connect to {self.host}:{self.port}: {exc}") from exc
            self._sock = sock
            try:
                self._authenticate()
            except Exception:
                self.close()
                raise
            log.info("EC connected to aMule %s at %s:%s", self.server_version, self.host, self.port)

    def close(self) -> None:
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                finally:
                    self._sock = None

    def __enter__(self) -> "ECClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _authenticate(self) -> None:
        req = Packet(C.OP_AUTH_REQ)
        req.add(Tag.string(C.TAG_CLIENT_NAME, self.client_name))
        req.add(Tag.string(C.TAG_CLIENT_VERSION, self.client_version))
        req.add(Tag.uint(C.TAG_PROTOCOL_VERSION, C.PROTOCOL_VERSION))
        if self.version_id:
            req.add(Tag.hash16(C.TAG_VERSION_ID, self.version_id))
        req.add(Tag.empty(C.TAG_CAN_ZLIB))
        req.add(Tag.empty(C.TAG_CAN_UTF8_NUMBERS))
        salt_reply = self._roundtrip(req)
        if salt_reply.opcode != C.OP_AUTH_SALT:
            raise ECError(f"handshake refused: {salt_reply.get_str(C.TAG_STRING) or salt_reply.dump()}")
        salt = salt_reply.get_int(C.TAG_PASSWD_SALT)
        salt_hash = hashlib.md5(f"{salt:X}".encode("ascii")).hexdigest()
        final = hashlib.md5((self._password_md5 + salt_hash).encode("ascii")).hexdigest()
        pw = Packet(C.OP_AUTH_PASSWD).add(Tag.hash16(C.TAG_PASSWD_HASH, final))
        ok = self._roundtrip(pw)
        if ok.opcode != C.OP_AUTH_OK:
            raise ECError(f"authentication failed: {ok.get_str(C.TAG_STRING) or ok.dump()}")
        self.server_version = ok.get_str(C.TAG_SERVER_VERSION)

    # ------------------------------------------------------------------
    # Low-level I/O
    # ------------------------------------------------------------------

    def _send(self, packet: Packet) -> None:
        assert self._sock is not None
        try:
            self._sock.sendall(encode_frame(packet, C.FLAG_BASE))
        except OSError as exc:
            self.close()
            raise ECConnectionError(f"send failed: {exc}") from exc

    def _recv_exact(self, n: int) -> bytes:
        assert self._sock is not None
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = self._sock.recv(n - len(buf))
            except OSError as exc:
                self.close()
                raise ECConnectionError(f"recv failed: {exc}") from exc
            if not chunk:
                self.close()
                raise ECConnectionError("connection closed by aMule")
            buf += chunk
        return bytes(buf)

    def _recv(self) -> Packet:
        flags, length = HEADER.unpack(self._recv_exact(HEADER.size))
        body = self._recv_exact(length)
        try:
            return decode_body(body, flags)
        except ECProtocolError:
            self.close()
            raise

    def _roundtrip(self, packet: Packet) -> Packet:
        with self._lock:
            if self._sock is None:
                raise ECConnectionError("not connected")
            self._send(packet)
            return self._recv()

    def request(self, packet: Packet, retry: bool = True) -> Packet:
        """Send a packet and return the reply, reconnecting once on failure.
        Raises ECError when the core answers OP_FAILED."""
        with self._lock:
            if self._sock is None:
                self.connect()
            try:
                reply = self._roundtrip(packet)
            except (ECConnectionError, ECProtocolError):
                if not retry:
                    raise
                log.warning("EC connection lost, reconnecting")
                self.connect()
                reply = self._roundtrip(packet)
        if reply.opcode == C.OP_FAILED:
            raise ECError(reply.get_str(C.TAG_STRING) or "request failed")
        return reply

    # ------------------------------------------------------------------
    # High-level API
    # ------------------------------------------------------------------

    def conn_state(self) -> ConnState:
        reply = self.request(Packet.new(C.OP_GET_CONNSTATE, C.DETAIL_CMD))
        tag = reply.find(C.TAG_CONNSTATE)
        if tag is None:
            raise ECError("no CONNSTATE tag in reply")
        bits = tag.get_int()
        server = tag.find(C.TAG_SERVER)
        return ConnState(
            ed2k_connected=bool(bits & 0x01),
            ed2k_connecting=bool(bits & 0x02),
            kad_connected=bool(bits & 0x04),
            kad_firewalled=bool(bits & 0x08),
            kad_running=bool(bits & 0x10),
            ed2k_id=tag.get_int(C.TAG_ED2K_ID),
            client_id=tag.get_int(C.TAG_CLIENT_ID),
            server_name=server.get_str(C.TAG_SERVER_NAME) if server else "",
        )

    def search_start(
        self,
        text: str,
        search_type: int = C.SEARCH_GLOBAL,
        file_type: str = "",
        extension: str = "",
        min_size: int = 0,
        max_size: int = 0,
        availability: int = 0,
    ) -> str:
        """Start a search. Returns the core's status string. Results are
        retrieved later with search_results()."""
        tag = Tag.uint(C.TAG_SEARCH_TYPE, search_type)
        tag.add(Tag.string(C.TAG_SEARCH_NAME, text))
        tag.add(Tag.string(C.TAG_SEARCH_FILE_TYPE, file_type))
        if extension:
            tag.add(Tag.string(C.TAG_SEARCH_EXTENSION, extension))
        if availability:
            tag.add(Tag.uint(C.TAG_SEARCH_AVAILABILITY, availability))
        if min_size:
            tag.add(Tag.uint(C.TAG_SEARCH_MIN_SIZE, min_size))
        if max_size:
            tag.add(Tag.uint(C.TAG_SEARCH_MAX_SIZE, max_size))
        reply = self.request(Packet(C.OP_SEARCH_START).add(tag))
        return reply.get_str(C.TAG_STRING)

    def search_stop(self) -> None:
        self.request(Packet(C.OP_SEARCH_STOP))

    def search_progress(self) -> int:
        """0..100, or 0xFFFF when no search is running."""
        reply = self.request(Packet(C.OP_SEARCH_PROGRESS))
        return reply.get_int(C.TAG_SEARCH_STATUS)

    def search_results(self) -> list[SearchResult]:
        reply = self.request(Packet.new(C.OP_SEARCH_RESULTS, C.DETAIL_FULL))
        results = []
        for tag in reply.find_all(C.TAG_SEARCHFILE):
            results.append(
                SearchResult(
                    ecid=tag.get_int(),
                    hash=tag.get_hash(C.TAG_PARTFILE_HASH),
                    name=tag.get_str(C.TAG_PARTFILE_NAME),
                    size=tag.get_int(C.TAG_PARTFILE_SIZE_FULL),
                    sources=tag.get_int(C.TAG_PARTFILE_SOURCE_COUNT),
                    complete_sources=tag.get_int(C.TAG_PARTFILE_SOURCE_COUNT_XFER),
                    status=tag.get_int(C.TAG_PARTFILE_STATUS),
                )
            )
        return results

    def download_search_result(self, file_hash: str, category: int = 0) -> None:
        """Queue a file from the current search results by its MD4 hash."""
        tag = Tag.hash16(C.TAG_PARTFILE, file_hash).add(Tag.uint(C.TAG_PARTFILE_CAT, category))
        self.request(Packet(C.OP_DOWNLOAD_SEARCH_RESULT).add(tag))

    def add_link(self, link: str, category: int = 0) -> None:
        """Add an ed2k:// or magnet link. Raises ECError if rejected."""
        tag = Tag.string(C.TAG_STRING, link)
        if category:
            tag.add(Tag.uint(C.TAG_PARTFILE_CAT, category))
        self.request(Packet(C.OP_ADD_LINK).add(tag))

    def download_queue(self) -> list[QueuedFile]:
        reply = self.request(Packet.new(C.OP_GET_DLOAD_QUEUE, C.DETAIL_FULL))
        files = []
        for tag in reply.find_all(C.TAG_PARTFILE):
            part_id = tag.get_int(C.TAG_PARTFILE_PARTMETID)
            files.append(
                QueuedFile(
                    ecid=tag.get_int(),
                    hash=tag.get_hash(C.TAG_PARTFILE_HASH),
                    name=tag.get_str(C.TAG_PARTFILE_NAME),
                    size=tag.get_int(C.TAG_PARTFILE_SIZE_FULL),
                    done=tag.get_int(C.TAG_PARTFILE_SIZE_DONE),
                    transferred=tag.get_int(C.TAG_PARTFILE_SIZE_XFER),
                    speed=tag.get_int(C.TAG_PARTFILE_SPEED),
                    status=tag.get_int(C.TAG_PARTFILE_STATUS),
                    stopped=bool(tag.get_int(C.TAG_PARTFILE_STOPPED)),
                    sources=tag.get_int(C.TAG_PARTFILE_SOURCE_COUNT),
                    sources_xfer=tag.get_int(C.TAG_PARTFILE_SOURCE_COUNT_XFER),
                    category=tag.get_int(C.TAG_PARTFILE_CAT),
                    part_met=f"{part_id:03d}.part.met" if part_id else "",
                    last_seen_complete=tag.get_int(C.TAG_PARTFILE_LAST_SEEN_COMP),
                    ed2k_link=tag.get_str(C.TAG_PARTFILE_ED2K_LINK),
                )
            )
        return files

    def _partfile_command(self, opcode: int, file_hash: str) -> None:
        self.request(Packet(opcode).add(Tag.hash16(C.TAG_PARTFILE, file_hash)))

    def pause(self, file_hash: str) -> None:
        self._partfile_command(C.OP_PARTFILE_PAUSE, file_hash)

    def resume(self, file_hash: str) -> None:
        self._partfile_command(C.OP_PARTFILE_RESUME, file_hash)

    def stop(self, file_hash: str) -> None:
        self._partfile_command(C.OP_PARTFILE_STOP, file_hash)

    def delete(self, file_hash: str) -> None:
        """Cancel and remove a download from the queue."""
        self._partfile_command(C.OP_PARTFILE_DELETE, file_hash)

    def clear_completed(self, ecids: list[int]) -> None:
        """Drop finished files from the core's completed-downloads list."""
        if not ecids:
            return
        packet = Packet(C.OP_CLEAR_COMPLETED)
        for ecid in ecids:
            packet.add(Tag.uint(C.TAG_ECID, ecid))
        self.request(packet)

    def directories(self) -> tuple[str, str]:
        """Return (incoming_dir, temp_dir) as configured in the core."""
        req = Packet(C.OP_GET_PREFERENCES).add(Tag.uint(C.TAG_SELECT_PREFS, C.PREFS_DIRECTORIES))
        reply = self.request(req)
        dirs = reply.find(C.TAG_PREFS_DIRECTORIES)
        if dirs is None:
            return "", ""
        return dirs.get_str(C.TAG_DIRECTORIES_INCOMING), dirs.get_str(C.TAG_DIRECTORIES_TEMP)

    def stats(self) -> dict[str, int]:
        reply = self.request(Packet.new(C.OP_STAT_REQ, C.DETAIL_CMD))
        return {
            "ul_speed": reply.get_int(C.TAG_STATS_UL_SPEED),
            "dl_speed": reply.get_int(C.TAG_STATS_DL_SPEED),
            "ul_limit": reply.get_int(C.TAG_STATS_UL_SPEED_LIMIT),
            "dl_limit": reply.get_int(C.TAG_STATS_DL_SPEED_LIMIT),
            "sources": reply.get_int(C.TAG_STATS_TOTAL_SRC_COUNT),
            "ul_queue": reply.get_int(C.TAG_STATS_UL_QUEUE_LEN),
            "ed2k_users": reply.get_int(C.TAG_STATS_ED2K_USERS),
            "kad_users": reply.get_int(C.TAG_STATS_KAD_USERS),
        }
