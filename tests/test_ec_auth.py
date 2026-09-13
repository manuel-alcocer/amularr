"""Handshake test against a fake aMule core speaking EC over a local socket."""

import hashlib
import socket
import threading

import pytest

from amularr.ec import ECClient, ECError
from amularr.ec import codes as C
from amularr.ec.tag import HEADER, Packet, Tag, decode_body, encode_frame

PASSWORD = "secret"
SALT = 0x1A2B3C4D5E6F7081


def read_packet(conn: socket.socket) -> Packet:
    header = b""
    while len(header) < HEADER.size:
        header += conn.recv(HEADER.size - len(header))
    flags, length = HEADER.unpack(header)
    body = b""
    while len(body) < length:
        body += conn.recv(length - len(body))
    return decode_body(body, flags)


class FakeCore(threading.Thread):
    def __init__(self, password_md5: str, server_flags: int):
        super().__init__(daemon=True)
        self.password_md5 = password_md5
        self.server_flags = server_flags
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.received: list[Packet] = []
        self.auth_ok = False

    def send(self, conn, packet):
        conn.sendall(encode_frame(packet, self.server_flags))

    def run(self):
        conn, _ = self.sock.accept()
        with conn:
            req = read_packet(conn)
            self.received.append(req)
            if req.opcode != C.OP_AUTH_REQ or req.get_int(C.TAG_PROTOCOL_VERSION) != C.PROTOCOL_VERSION:
                self.send(conn, Packet(C.OP_AUTH_FAIL).add(Tag.string(C.TAG_STRING, "bad protocol")))
                return
            self.send(conn, Packet(C.OP_AUTH_SALT).add(Tag.uint(C.TAG_PASSWD_SALT, SALT)))
            pw = read_packet(conn)
            self.received.append(pw)
            salt_hash = hashlib.md5(f"{SALT:X}".encode()).hexdigest()
            expected = hashlib.md5((self.password_md5 + salt_hash).encode()).hexdigest().upper()
            if pw.opcode == C.OP_AUTH_PASSWD and pw.find(C.TAG_PASSWD_HASH).get_hash() == expected:
                self.auth_ok = True
                self.send(conn, Packet(C.OP_AUTH_OK).add(Tag.string(C.TAG_SERVER_VERSION, "fake 1.0")))
            else:
                self.send(conn, Packet(C.OP_AUTH_FAIL).add(Tag.string(C.TAG_STRING, "Authentication failed.")))
                return
            # one more request: search progress
            req = read_packet(conn)
            self.received.append(req)
            self.send(conn, Packet(C.OP_SEARCH_PROGRESS).add(Tag.uint(C.TAG_SEARCH_STATUS, 42)))


@pytest.mark.parametrize("server_flags", [C.FLAG_BASE, C.FLAG_BASE | C.FLAG_UTF8_NUMBERS | C.FLAG_ZLIB])
def test_handshake_success(server_flags):
    core = FakeCore(hashlib.md5(PASSWORD.encode()).hexdigest(), server_flags)
    core.start()
    with ECClient("127.0.0.1", core.port, password=PASSWORD, timeout=5) as client:
        assert client.server_version == "fake 1.0"
        assert client.search_progress() == 42
    core.join(5)
    assert core.auth_ok
    assert core.received[0].get_str(C.TAG_CLIENT_NAME) == "amularr"


def test_handshake_wrong_password():
    core = FakeCore(hashlib.md5(b"other").hexdigest(), C.FLAG_BASE)
    core.start()
    client = ECClient("127.0.0.1", core.port, password=PASSWORD, timeout=5)
    with pytest.raises(ECError, match="Authentication failed"):
        client.connect()
    assert not client.connected
